# The MIT License (MIT)
#
# Copyright (c) 2017-2021 CNRS
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import math
from copy import deepcopy
from itertools import combinations
from typing import List, Optional, Text, Tuple

import numpy as np
import torch
import torch.nn.functional as F
from scipy.cluster.hierarchy import fcluster

from pyannote.audio import Inference
from pyannote.audio.core.io import AudioFile
from pyannote.audio.core.pipeline import Pipeline
from pyannote.audio.pipelines.utils import PipelineModel, get_devices, get_model
from pyannote.audio.utils.signal import Binarize
from pyannote.core import Annotation, SlidingWindow, SlidingWindowFeature
from pyannote.core.utils.hierarchy import pool
from pyannote.metrics.diarization import GreedyDiarizationErrorRate
from pyannote.pipeline.parameter import Uniform

from .resegmentation import Resegmentation


class SpeakerDiarization(Pipeline):
    """Speaker diarization pipeline

    1. Apply the pretrained segmentation model S on sliding chunks
    2. Use a heuristic to remove "noisy" chunks (e.g. those on which S is not very confident)
    3. Apply the pretrained embedding model E to get one embedding per ("clean" chunk, active speaker) pair
    4. Apply hierarchical agglomerative clustering on those embeddings while (optionnally) preventing two
       speakers from the same chunk to end up in the same cluster (cannot-link constraints)
    5. Use this ("clean" chunks only) diarization as labels to adapt segmentation model S into a speaker tracking model T
    6. Apply the fine-tuned speaker tracking model T on sliding chunks and assign each (chunk, active speaker) pair
       to the most likely speaker.

    Parameters
    ----------
    segmentation : Model or str, optional
        `Inference` instance used to extract raw segmentation scores.
        When `str`, assumes that file already contains a corresponding key with
        precomputed scores. Defaults to "seg".
    layers : list, optional
        Only fine-tune those layers, unfreezing them in that order.
        Defaults to fine-tuning all layers from output layer to input layer.
    embeddings : Inference or str, optional
        `Inference` instance used to extract speaker embeddings. When `str`,
        assumes that file already contains a corresponding key with precomputed
        embeddings. Defaults to "emb".
    metric : {'euclidean', 'cosine', 'angular'}, optional
        Metric used for comparing embeddings. Defaults to 'cosine'.
    use_cannot_link_constraints : bool, optional
        Whether to use same-chunk cannot-link constraints. Defaults to True.

    Hyper-parameters
    ----------------
    activity_threshold : float between 0 and 1
        A speaker is considered active as soon as one frame goes above `activity_threshold`.
    confidence_threshold : float between 0 and 1
        Segmentation model is considered confident on a given chunk if its confidence vlaue
        goes above `confidence_threshold`. See code below to understand the heuristic uses
        to compute this confidence measure.
    clustering_threshold : float between 0 and 2 (in case of 'cosine' metric)
        Agglomerative clustering stopping criterion.
    """

    def __init__(
        self,
        segmentation: PipelineModel = "pyannote/Segmentation-PyanNet-DIHARD",
        layers: List[Text] = None,
        embedding: PipelineModel = "hbredin/SpeakerEmbedding-XVectorMFCC-VoxCeleb",
        metric: Optional[str] = "cosine",
        use_cannot_link_constraints: bool = True,
    ):

        super().__init__()

        self.segmentation = segmentation
        self.layers = layers
        self.embedding = embedding
        self.metric = metric
        self.use_cannot_link_constraints = use_cannot_link_constraints

        segmentation_device, embedding_device = get_devices(needs=2)
        self.seg_model_ = get_model(segmentation).to(segmentation_device)
        self.emb_model_ = get_model(embedding).to(embedding_device)
        # NOTE: `get_model` takes care of calling model.eval()

        # audio reader used by segmentation model
        self.audio_ = self.seg_model_.audio
        # duration of chunks (in seconds) given as input of segmentation model
        self.seg_chunk_duration_ = self.seg_model_.specifications.duration
        # step between two consecutive chunks (as ratio of chunk duration)
        # TODO: study the effect of this parameter on clustering with "cannot-link" constraints
        self.seg_chunk_step_ratio_ = 1.0
        # number of speakers in output of segmentation model
        self.seg_num_speakers_ = len(self.seg_model_.specifications.classes)
        # duration of a frame (in seconds) in output of segmentation model
        self.seg_frame_duration_ = (
            self.seg_model_.introspection.inc_num_samples / self.audio_.sample_rate
        )
        # output frames as SlidingWindow instances
        self.seg_frames_ = SlidingWindow(
            start=0.0, step=self.seg_frame_duration_, duration=self.seg_frame_duration_
        )

        # prepare segmentation model for inference
        self.segmentation_inference_ = Inference(
            self.seg_model_,
            window="sliding",
            skip_aggregation=True,
            duration=self.seg_chunk_duration_,
            step=self.seg_chunk_step_ratio_ * self.seg_chunk_duration_,
            batch_size=32,
        )

        # will be used to go from speaker activations SlidingWindowFeature instance
        # to an actual diarization (as Annotation instance).
        self.binarize_ = Binarize(
            onset=0.5,
            offset=0.5,
            min_duration_on=0.0,
            min_duration_off=0.0,
            pad_onset=0.0,
            pad_offset=0.0,
        )

        self.resegmentation = Resegmentation(
            segmentation=self.segmentation_inference_,
            layers=self.layers,
            diarization="partial_diarization",
            confidence="reliable_frames",
        )

        # hyperparameters
        self.activity_threshold = Uniform(0.5, 1.0)
        self.confidence_threshold = Uniform(0.5, 1.0)
        self.clustering_threshold = Uniform(0.5, 1.5)

    @staticmethod
    def pooling_func(
        u: int,
        v: int,
        C: np.ndarray = None,
        **kwargs,
    ) -> np.ndarray:
        """Compute average of newly merged cluster

        Parameters
        ----------
        u : int
            Cluster index.
        v : int
            Cluster index.
        C : (2 x n_observations - 1, dimension) np.ndarray
            Cluster embedding.

        Returns
        -------
        Cuv : (dimension, ) np.ndarray
            Embedding of newly formed cluster.
        """
        return C[u] + C[v]

    def apply(self, file: AudioFile) -> Annotation:
        """Apply speaker diarization

        Parameters
        ----------
        file : AudioFile
            Processed file.

        Returns
        -------
        diarization : Annotation
            Speaker diarization
        """

        # =====================================================================
        # Apply the pretrained segmentation model S on sliding chunks.
        # =====================================================================

        # output of segmentation model on each chunk
        segmentations: SlidingWindowFeature = self.segmentation_inference_(file)
        # TODO: don't use left- and right-most part of each chunk
        # as this is where the model is usually not that good

        # number of frames in each chunk
        num_frames_in_chunk = segmentations.data.shape[1]
        # number of frames in the whole file
        num_frames_in_file = math.ceil(
            self.audio_.get_duration(file) / self.seg_frame_duration_
        )

        # =====================================================================
        # Use a heuristic to remove "bad" chunks.
        # here: those on which S is not very confident
        # =====================================================================

        # confidence of segmentation model on each chunk
        # shape = (num_chunks, )
        confidences = np.mean(
            np.min((np.abs(segmentations.data - 0.5) / 0.5), axis=2), axis=1
        )
        # TODO: use a different heuristic to also penalize chunks with overlap
        # TODO: make confidence_threshold a percentile rather than an absolute value

        # will eventually contain stacked embeddings of each ("good" chunk, active speaker) pair
        embeddings = []
        # will eventually contain the list of chunk indices of each ("good" chunk, active speaker) pair
        chunk_indices = []
        # will eventually contain the list of speaker indices of each ("good" chunk, active speaker) pair
        active_speaker_indices = []
        # will eventually contain the list of chunk indices where the segmentation model is confident no-one speaks
        inactive_chunk_indices = []
        # will eventually contain the list of embedding indices that cannot be merged
        # because they come from the same chunk -- hence must be two different speakers.
        cannot_link: List[Tuple[int, int]] = []
        # counter for the total number of ("good" chunk, active speaker) pairs
        k = 0

        # =====================================================================
        # Apply the pretrained embedding model E to get one embedding
        # per ("good" chunk, active speaker) pair
        # =====================================================================

        for c, (chunk, segmentation) in enumerate(segmentations):
            # shape of segmentation is (num_frames, num_speakers)

            # skip "bad" chunks as they are likely to lead to bad clustering
            if confidences[c] < self.confidence_threshold:
                continue

            # a speaker is decide to be "active" as soon as its activation goes
            # above `activity_threshold` (even if it is for just one frame)
            is_active = np.max(segmentation, axis=0) > self.activity_threshold

            # number of active speakers in current chunk
            num_active_speakers_in_chunk = np.sum(is_active)

            # skip (and remember) chunks where there is no active speaker
            if num_active_speakers_in_chunk < 1:
                inactive_chunk_indices.append(c)
                continue

            # keep track of chunk and active speaker indices for post-clustering reconstruction
            chunk_indices.append(num_active_speakers_in_chunk * [c])
            active_speaker_indices.append(np.where(is_active)[0])
            k += num_active_speakers_in_chunk

            # extract speaker embeddings
            with torch.no_grad():

                # read audio chunk
                waveform = self.audio_.crop(file, chunk)[0].unsqueeze(dim=0)
                # shape (1, num_channels == 1, num_samples == chunk_duration x sample_rate)

                # we give more weights to regions where speaker is active
                # TODO: give more weights to non-overlapping regions
                # TODO: give more weights in the middle of chunks
                weights = torch.tensor(segmentation).T
                # shape (num_speakers, num_frames)

                # forward pass + L2 normalization
                embedding = F.normalize(
                    self.emb_model_(
                        waveform.repeat(self.seg_num_speakers_, 1, 1).to(
                            self.emb_model_.device
                        ),
                        weights=weights.to(self.emb_model_.device),
                    ),
                    dim=1,
                ).cpu()
                # shape (num_speakers, emb_dimension)

                # scale embeddings by the actual speech duration of each speaker
                scales = np.mean(segmentation, axis=0, keepdims=True)
                # shape (num_speakers, 1)
                embedding *= scales.T

                embeddings.append(embedding[is_active])

            # cannot-link constraints prevent merging two speakers
            # from the same chunk
            for (i, j) in combinations(range(num_active_speakers_in_chunk), 2):
                cannot_link.append((k + i, k + j))

        # handle corner case where there is strictly less than two ("good" chunk, active speaker) pair
        if len(embeddings) < 2:
            return Annotation(uri=file["uri"])

        # stack everything as numpy arrays
        embeddings = np.vstack(embeddings)
        chunk_indices = np.hstack(chunk_indices)
        active_speaker_indices = np.hstack(active_speaker_indices)

        # =====================================================================
        # Apply hierarchical agglomerative clustering on embeddings and prevent
        # two speakers from the same chunk to end up in the same cluster
        # =====================================================================

        # hierarchical agglomerative clustering with "pool" linkage
        Z = pool(
            embeddings,
            metric=self.metric,
            pooling_func=self.pooling_func,
            cannot_link=cannot_link if self.use_cannot_link_constraints else None,
        )
        clusters = fcluster(Z, self.clustering_threshold, criterion="distance")
        num_clusters = len(np.unique(clusters))

        # use clustering result to assign each active speaker segmentation score
        # to the "right" speaker.
        aggregated = np.zeros((num_frames_in_file, num_clusters))
        overlapped = np.zeros((num_frames_in_file, num_clusters))
        chunks = segmentations.sliding_window
        for cluster, c, a in zip(clusters, chunk_indices, active_speaker_indices):
            # add original segmentation score to the "right" speaker
            start_frame = round(chunks[c].start / self.seg_frame_duration_)
            aggregated[
                start_frame : start_frame + num_frames_in_chunk, cluster - 1
            ] += segmentations.data[c, :, a]
            # remember how many chunks were added on this particular speaker
            overlapped[
                start_frame : start_frame + num_frames_in_chunk, cluster - 1
            ] += 1.0

        # also keep track of chunks where no-one speaks as this is important
        # information as well for the subsequent speaker tracking step
        for c in inactive_chunk_indices:
            start_frame = round(chunks[c].start / self.seg_frame_duration_)
            overlapped[start_frame : start_frame + num_frames_in_chunk] += 1.0

        # make sure that, when at least one speaker is active, other speakers
        # are considered inactive (and not just "bad" chunks).
        # this is done by setting `overlapped` indices to (at least) 1.
        any_active = np.mean(overlapped, axis=1) > 0
        overlapped[any_active] = np.maximum(1.0, overlapped[any_active])

        partial_speaker_activations = SlidingWindowFeature(
            aggregated / overlapped, self.seg_frames_
        )

        # =====================================================================
        # Use this ("good" chunks) diarization as labels to adapt
        # segmentation model S into a speaker tracking model T
        # =====================================================================

        # we use the fact that partial_speaker_activations is NaN on (previously
        # ignored) "bad" chunks to mark those frames as unreliable for training
        # the speaker tracking model
        reliable_frames = deepcopy(partial_speaker_activations)
        reliable_frames.data = 1.0 * (
            np.mean(np.isnan(reliable_frames.data), axis=1, keepdims=True) < 1.0
        )
        file["reliable_frames"] = reliable_frames

        # now that reliable regions are known, we can replace NaNs by 0,
        # and binarize speaker activations to get a partial (on "good" chunks)
        # diarization
        partial_speaker_activations.data[np.isnan(partial_speaker_activations.data)] = 0
        file["partial_diarization"] = self.binarize_(partial_speaker_activations)

        file["resegmentation"] = self.resegmentation.apply(file)
        return self.binarize_(file["resegmentation"])

    def get_metric(self) -> GreedyDiarizationErrorRate:
        return GreedyDiarizationErrorRate(collar=0.0, skip_overlap=False)
