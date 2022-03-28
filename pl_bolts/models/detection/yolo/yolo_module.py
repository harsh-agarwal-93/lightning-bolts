from copy import copy
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import torch
import torch.nn as nn
from pytorch_lightning import LightningModule
from pytorch_lightning.utilities.cli import LightningCLI
from torch import Tensor, optim
from torchmetrics.detection.map import MAP

from pl_bolts.datamodules import VOCDetectionDataModule
from pl_bolts.datamodules.vocdetection_datamodule import Compose
from pl_bolts.models.detection.yolo.darknet_network import DarknetNetwork
from pl_bolts.optimizers.lr_scheduler import LinearWarmupCosineAnnealingLR
from pl_bolts.utils import _TORCHVISION_AVAILABLE
from pl_bolts.utils.warnings import warn_missing_pkg

if _TORCHVISION_AVAILABLE:
    from torchvision.ops import batched_nms
    from torchvision.transforms import functional as F
else:
    warn_missing_pkg("torchvision")


class YOLO(LightningModule):
    """PyTorch Lightning implementation of YOLO that supports the most important features of YOLOv3, YOLOv4,
    YOLOv5, Scaled-YOLOv4, and YOLOX.

    *YOLOv3 paper*: `Joseph Redmon and Ali Farhadi <https://arxiv.org/abs/1804.02767>`_

    *YOLOv4 paper*: `Alexey Bochkovskiy, Chien-Yao Wang, and Hong-Yuan Mark Liao <https://arxiv.org/abs/2004.10934>`_

    *Scaled-YOLOv4 paper*: `Chien-Yao Wang, Alexey Bochkovskiy, and Hong-Yuan Mark Liao
    <https://arxiv.org/abs/2011.08036>`_

    *YOLOX paper*: `Zheng Ge, Songtao Liu, Feng Wang, Zeming Li, and Jian Sun <https://arxiv.org/abs/2107.08430>`_

    *Implementation*: `Seppo Enarvi <https://github.com/senarvi>`_

    The network architecture can be written in PyTorch, or read from a Darknet configuration file using the
    :class:`~pl_bolts.models.detection.yolo.darknet_network.DarknetNetwork` class. ``DarknetNetwork`` is also able to
    read weights from a Darknet model file.

    The input from the data loader is expected to be a list of images. Each image is a tensor with shape
    ``[channels, height, width]``. The images from a single batch will be stacked into a single tensor, so the sizes
    have to match. Different batches can have different image sizes, as long as the size is divisible by the ratio in
    which the network downsamples the input.

    During training, the model expects both the input tensors and a list of targets. *Each target is a dictionary
    containing the following tensors*:

    - boxes (``FloatTensor[N, 4]``): the ground-truth boxes in `(x1, y1, x2, y2)` format
    - labels (``Int64Tensor[N]`` or ``BoolTensor[N, classes]``): the class label or a boolean class mask for each
      ground-truth box

    :func:`~pl_bolts.models.detection.yolo.yolo_module.YOLO.forward` method returns all predictions from all detection
    layers in one tensor with shape ``[images, predictors, classes + 5]``. The coordinates are scaled to the input image
    size. During training it also returns a dictionary containing the classification, box overlap, and confidence
    losses.

    During inference, the model requires only the image tensors.
    :func:`~pl_bolts.models.detection.yolo.yolo_module.YOLO.infer` method filters and processes the predictions. If a
    prediction has a high score for more than one class, it will be duplicated. *The processed output is returned in a
    dictionary containing the following tensors*:

    - boxes (``FloatTensor[N, 4]``): predicted bounding box `(x1, y1, x2, y2)` coordinates in image space
    - scores (``FloatTensor[N]``): detection confidences
    - labels (``Int64Tensor[N]``): the predicted labels for each object

    Args:
        network: A module that represents the network layers. This can be obtained from a Darknet configuration using
            :func:`~pl_bolts.models.detection.yolo.darknet_network.DarknetNetwork`, or it can be defined as PyTorch
            code.
        optimizer: Which optimizer class to use for training.
        optimizer_params: Parameters to pass to the optimizer constructor. Weight decay will be applied only to
            convolutional layer weights.
        lr_scheduler: Which learning rate scheduler class to use for training.
        lr_scheduler_params: Parameters to pass to the learning rate scheduler constructor.
        confidence_threshold: Postprocessing will remove bounding boxes whose confidence score is not higher than this
            threshold.
        nms_threshold: Non-maximum suppression will remove bounding boxes whose IoU with a higher confidence box is
            higher than this threshold, if the predicted categories are equal.
        detections_per_image: Keep at most this number of highest-confidence detections per image.
    """

    def __init__(
        self,
        network: nn.ModuleList,
        optimizer: Type[optim.Optimizer] = optim.SGD,
        optimizer_params: Dict[str, Any] = {"lr": 0.01, "momentum": 0.9, "weight_decay": 0.0005},
        lr_scheduler: Type[optim.lr_scheduler._LRScheduler] = LinearWarmupCosineAnnealingLR,
        lr_scheduler_params: Dict[str, Any] = {"warmup_epochs": 5, "max_epochs": 300, "warmup_start_lr": 0.0},
        confidence_threshold: float = 0.2,
        nms_threshold: float = 0.45,
        detections_per_image: int = 300,
    ) -> None:
        super().__init__()

        if not _TORCHVISION_AVAILABLE:
            raise ModuleNotFoundError("YOLO model uses `torchvision`, which is not installed yet.")  # pragma: no-cover

        self.network = network
        self.optimizer_class = optimizer
        self.optimizer_params = optimizer_params
        self.lr_scheduler_class = lr_scheduler
        self.lr_scheduler_params = lr_scheduler_params
        self.confidence_threshold = confidence_threshold
        self.nms_threshold = nms_threshold
        self.detections_per_image = detections_per_image

        self._val_map = MAP(compute_on_step=False)
        self._test_map = MAP(compute_on_step=False)

    def forward(self, images: Tensor, targets: Optional[List[Dict[str, Tensor]]] = None) -> Tuple[Tensor, Tensor]:
        """Runs a forward pass through the network (all layers listed in ``self.network``), and if training targets
        are provided, computes the losses from the detection layers.

        Detections are concatenated from the detection layers. Each detection layer will produce a number of detections
        that depends on the size of the feature map and the number of anchors per feature map cell.

        Args:
            images: Images to be processed. Tensor of size
                ``[batch_size, channels, height, width]``.
            targets: If set, computes losses from detection layers against these targets. A list of
                target dictionaries, one for each image.

        Returns:
            detections (:class:`~torch.Tensor`), losses (Dict[str, :class:`~torch.Tensor`]): Detections, and if targets
            were provided, a dictionary of losses. Detections are shaped
            ``[batch_size, predictors, classes + 5]``, where ``predictors`` is the total number of feature map cells in
            all detection layers times the number of anchors per cell. The predicted box coordinates are in
            `(x1, y1, x2, y2)` format and scaled to the input image size.
        """
        detections, losses, hits = self.network(images, targets)

        detections = torch.cat(detections, 1)
        if targets is None:
            return detections

        total_hits = sum(hits)
        for layer_idx, layer_hits in enumerate(hits):
            hit_rate = torch.true_divide(layer_hits, total_hits) if total_hits > 0 else 1.0
            self.log(f"layer_{layer_idx}_hit_rate", hit_rate, sync_dist=False)

        losses = torch.stack(losses).sum(0)
        return detections, losses

    def configure_optimizers(self) -> Tuple[List, List]:
        """Constructs the optimizer and learning rate scheduler based on ``self.optimizer_params`` and
        ``self.lr_scheduler_params``.

        If weight decay is specified, it will be applied only to convolutional layer weights.
        """
        if ("weight_decay" in self.optimizer_params) and (self.optimizer_params["weight_decay"] != 0):
            defaults = copy(self.optimizer_params)
            weight_decay = defaults.pop("weight_decay")

            default_group = []
            wd_group = []
            for name, tensor in self.named_parameters():
                if name.endswith(".conv.weight"):
                    wd_group.append(tensor)
                else:
                    default_group.append(tensor)

            params = [
                {"params": default_group, "weight_decay": 0.0},
                {"params": wd_group, "weight_decay": weight_decay},
            ]
            optimizer = self.optimizer_class(params, **defaults)
        else:
            optimizer = self.optimizer_class(self.parameters(), **self.optimizer_params)
        lr_scheduler = self.lr_scheduler_class(optimizer, **self.lr_scheduler_params)
        return [optimizer], [lr_scheduler]

    def training_step(self, batch: Tuple[List[Tensor], List[Dict[str, Tensor]]], batch_idx: int) -> Dict[str, Tensor]:
        """Computes the training loss.

        Args:
            batch: A tuple of images and targets. Images is a list of 3-dimensional tensors. Targets is a list of target
                dictionaries.
            batch_idx: The index of this batch.

        Returns:
            A dictionary that includes the training loss in 'loss'.
        """
        images, targets = self._validate_batch(batch)
        _, losses = self(images, targets)

        # sync_dist=True is broken in some versions of Lightning and may cause the sum of the loss
        # across GPUs to be returned.
        self.log("train/overlap_loss", losses[0], prog_bar=True, sync_dist=False)
        self.log("train/confidence_loss", losses[1], prog_bar=True, sync_dist=False)
        self.log("train/class_loss", losses[2], prog_bar=True, sync_dist=False)
        self.log("train/total_loss", losses.sum(), sync_dist=False)

        return {"loss": losses.sum()}

    def validation_step(self, batch: Tuple[List[Tensor], List[Dict[str, Tensor]]], batch_idx: int):
        """Evaluates a batch of data from the validation set.

        Args:
            batch: A tuple of images and targets. Images is a list of 3-dimensional tensors. Targets is a list of target
                dictionaries.
            batch_idx: The index of this batch
        """
        images, targets = self._validate_batch(batch)
        detections, losses = self(images, targets)

        self.log("val/overlap_loss", losses[0], sync_dist=True)
        self.log("val/confidence_loss", losses[1], sync_dist=True)
        self.log("val/class_loss", losses[2], sync_dist=True)
        self.log("val/total_loss", losses.sum(), sync_dist=True)

        detections = self.process_detections(detections)
        targets = self.process_targets(targets)
        self._val_map(detections, targets)

    def validation_epoch_end(self, outputs):
        map_scores = self._val_map.compute()
        map_scores = {"val/" + k: v for k, v in map_scores.items()}
        self.log_dict(map_scores, sync_dist=True)
        self._val_map.reset()

    def test_step(self, batch: Tuple[List[Tensor], List[Dict[str, Tensor]]], batch_idx: int):
        """Evaluates a batch of data from the test set.

        Args:
            batch: A tuple of images and targets. Images is a list of 3-dimensional tensors. Targets is a list of target
                dictionaries.
            batch_idx: The index of this batch.
        """
        images, targets = self._validate_batch(batch)
        detections, losses = self(images, targets)

        self.log("test/overlap_loss", losses[0], sync_dist=True)
        self.log("test/confidence_loss", losses[1], sync_dist=True)
        self.log("test/class_loss", losses[2], sync_dist=True)
        self.log("test/total_loss", losses.sum(), sync_dist=True)

        detections = self.process_detections(detections)
        targets = self.process_targets(targets)
        self._test_map(detections, targets)

    def test_epoch_end(self, outputs):
        map_scores = self._test_map.compute()
        map_scores = {"test/" + k: v for k, v in map_scores.items()}
        self.log_dict(map_scores, sync_dist=True)
        self._test_map.reset()

    def infer(self, image: Tensor) -> Dict[str, Tensor]:
        """Feeds an image to the network and returns the detected bounding boxes, confidence scores, and class
        labels.

        If a prediction has a high score for more than one class, it will be duplicated.

        Args:
            image: An input image, a tensor of uint8 values sized ``[channels, height, width]``.

        Returns:
            A dictionary containing tensors "boxes", "scores", and "labels". "boxes" is a matrix of detected bounding
            box `(x1, y1, x2, y2)` coordinates. "scores" is a vector of confidence scores for the bounding box
            detections. "labels" is a vector of predicted class labels.
        """
        if not isinstance(image, torch.Tensor):
            image = F.to_tensor(image)

        self.eval()
        detections = self(image.unsqueeze(0))
        detections = self.process_detections(detections)
        return detections[0]

    def process_detections(self, preds: Tensor) -> List[Dict[str, Tensor]]:
        """Splits the detection tensor returned by a forward pass into a list of prediction dictionaries, and
        filters them based on confidence threshold, non-maximum suppression (NMS), and maximum number of
        predictions.

        If for any single detection there are multiple categories whose score is above the confidence threshold, the
        detection will be duplicated to create one detection for each category. NMS processes one category at a time,
        iterating over the bounding boxes in descending order of confidence score, and removes lower scoring boxes that
        have an IoU greater than the NMS threshold with a higher scoring box.

        The returned detections are sorted by descending confidence. The items of the dictionaries are as follows:

        - boxes (``Tensor[batch_size, N, 4]``): detected bounding box `(x1, y1, x2, y2)` coordinates
        - scores (``Tensor[batch_size, N]``): detection confidences
        - labels (``Int64Tensor[batch_size, N]``): the predicted class IDs

        Args:
            preds: A tensor of detected bounding boxes and their attributes.

        Returns:
            Filtered detections. A list of prediction dictionaries, one for each image.
        """
        result = []

        for image_preds in preds:
            boxes = image_preds[..., :4]
            confidences = image_preds[..., 4]
            classprobs = image_preds[..., 5:]
            scores = classprobs * confidences[:, None]

            # Select predictions with high scores. If a prediction has a high score for more than one class, it will be
            # duplicated.
            idxs, labels = (scores > self.confidence_threshold).nonzero().T
            boxes = boxes[idxs]
            scores = scores[idxs, labels]

            keep = batched_nms(boxes, scores, labels, self.nms_threshold)
            keep = keep[: self.detections_per_image]
            boxes = boxes[keep]
            scores = scores[keep]
            labels = labels[keep]
            result.append({"boxes": boxes, "scores": scores, "labels": labels})

        return result

    def process_targets(self, targets: List[Dict[str, Tensor]]) -> List[Dict[str, Tensor]]:
        """Duplicates multi-label targets to create one target for each label.

        Args:
            targets: List of target dictionaries. Each dictionary must contain "boxes" and "labels". "labels" is either
                a one-dimensional list of class IDs, or a two-dimensional boolean class map.

        Returns:
            Single-label targets. A list of target dictionaries, one for each image.
        """
        result = []

        for image_targets in targets:
            boxes = image_targets["boxes"]
            labels = image_targets["labels"]
            if labels.ndim == 2:
                idxs, labels = labels.nonzero().T
                boxes = boxes[idxs]
            result.append({"boxes": boxes, "labels": labels})

        return result

    def _validate_batch(
        self, batch: Tuple[List[Tensor], List[Dict[str, Tensor]]]
    ) -> Tuple[Tensor, List[Dict[str, Tensor]]]:
        """Reads a batch of data, validates the format, and stacks the images into a single tensor.

        Args:
            batch: The batch of data read by the :class:`~torch.utils.data.DataLoader`.

        Returns:
            The input batch with images stacked into a single tensor.
        """
        images, targets = batch

        if len(images) != len(targets):
            raise ValueError(f"Got {len(images)} images, but targets for {len(targets)} images.")

        for image in images:
            if not isinstance(image, Tensor):
                raise ValueError(f"Expected image to be of type Tensor, got {type(image)}.")

        for target in targets:
            boxes = target["boxes"]
            if not isinstance(boxes, Tensor):
                raise ValueError(f"Expected target boxes to be of type Tensor, got {type(boxes)}.")
            if (boxes.ndim != 2) or (boxes.shape[-1] != 4):
                raise ValueError(f"Expected target boxes to be tensors of shape [N, 4], got {list(boxes.shape)}.")
            labels = target["labels"]
            if not isinstance(labels, Tensor):
                raise ValueError(f"Expected target labels to be of type Tensor, got {type(labels)}.")
            if (labels.ndim < 1) or (labels.ndim > 2) or (len(labels) != len(boxes)):
                raise ValueError(
                    f"Expected target labels to be tensors of shape [N] or [N, num_classes], got {list(labels.shape)}."
                )

        images = torch.stack(images)
        return images, targets


class DarknetYOLO(YOLO):
    """A subclass of YOLO that uses a Darknet configuration file and can be configured using LightningCLI.

    At most one matching algorithm, ``match_sim_ota``, ``match_size_ratio``, or ``match_iou_threshold`` can be
    specified. If none of them is given, the default algorithm is used, which matches a target to the prior shape
    (anchor) that gives the highest IoU.

    CLI command::

        # PascalVOC using LightningCLI
        wget https://raw.githubusercontent.com/AlexeyAB/darknet/master/cfg/yolov4-tiny-3l.cfg
        python yolo_module.py fit --model.network_config yolov4-tiny-3l.cfg --data.batch_size 8 --trainer.gpus 8 \
            --trainer.accumulate_grad_batches 2

    Args:
        network_config: Path to a Darknet configuration file that defines the network architecture.
        match_sim_ota: If ``True``, matches a target to an anchor using the SimOTA algorithm from YOLOX.
        match_size_ratio: If specified, matches a target to an anchor if its width and height relative to the anchor is
            smaller than this ratio. If ``match_size_ratio`` or ``match_iou_threshold`` is not specified, selects for
            each target the anchor with the highest IoU.
        match_iou_threshold: If specified, matches a target to an anchor if the IoU is higher than this threshold.
        ignore_bg_threshold: If a predictor is not responsible for predicting any target, but the prior shape has IoU
            with some target greater than this threshold, the predictor will not be taken into account when calculating
            the confidence loss.
        overlap_func: Which function to use for calculating the overlap between boxes. Valid values are "iou", "giou",
            "diou", and "ciou".
        predict_overlap: Balance between binary confidence targets and predicting the overlap. 0.0 means that target
            confidence is one if there's an object, and 1.0 means that the target confidence is the output of
            ``overlap_func``.
        overlap_loss_multiplier: Overlap loss will be scaled by this value.
        class_loss_multiplier: Classification loss will be scaled by this value.
        confidence_loss_multiplier: Confidence loss will be scaled by this value.
    """

    def __init__(
        self,
        network_config: str,
        darknet_weights: Optional[str] = None,
        match_sim_ota: bool = False,
        match_size_ratio: Optional[float] = None,
        match_iou_threshold: Optional[float] = None,
        ignore_bg_threshold: Optional[float] = None,
        overlap_func: Optional[str] = None,
        predict_overlap: Optional[float] = None,
        overlap_loss_multiplier: Optional[float] = None,
        class_loss_multiplier: Optional[float] = None,
        confidence_loss_multiplier: Optional[float] = None,
        **kwargs,
    ) -> None:
        network = DarknetNetwork(
            network_config,
            darknet_weights,
            match_sim_ota=match_sim_ota,
            match_size_ratio=match_size_ratio,
            match_iou_threshold=match_iou_threshold,
            ignore_bg_threshold=ignore_bg_threshold,
            overlap_func=overlap_func,
            predict_overlap=predict_overlap,
            overlap_loss_multiplier=overlap_loss_multiplier,
            class_loss_multiplier=class_loss_multiplier,
            confidence_loss_multiplier=confidence_loss_multiplier,
        )
        super().__init__(**kwargs, network=network)


class ResizedVOCDetectionDataModule(VOCDetectionDataModule):
    """A subclass of VOCDetectionDataModule that resizes the images to a specific size. YOLO expectes the image
    size to be divisible by the ratio in which the network downsamples the image.

    Args:
        width: Resize images to this width.
        height: Resize images to this height.
    """

    def __init__(self, width: int = 608, height: int = 608, **kwargs):
        super().__init__(**kwargs)
        self.image_size = (height, width)

    def default_transforms(self) -> Callable:
        transforms = [
            lambda image, target: (F.to_tensor(image), target),
            self._resize,
        ]
        if self.normalize:
            transforms += [
                lambda image, target: (
                    F.normalize(image, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                    target,
                )
            ]
        return Compose(transforms)

    def _resize(self, image: Tensor, target: Dict[str, Any]):
        """Rescales the image and target to ``self.image_size``.

        Args:
            tensor: Image tensor to be resized.
            target: Dictionary of detection targets.

        Returns:
            Resized image tensor.
        """
        device = target["boxes"].device
        height, width = image.shape[-2:]
        original_size = torch.tensor([height, width], device=device)
        scale_y, scale_x = torch.tensor(self.image_size, device=device) / original_size
        scale = torch.tensor([scale_x, scale_y, scale_x, scale_y], device=device)
        image = F.resize(image, self.image_size)
        target["boxes"] = target["boxes"] * scale
        return image, target


if __name__ == "__main__":
    LightningCLI(DarknetYOLO, ResizedVOCDetectionDataModule, seed_everything_default=42)
