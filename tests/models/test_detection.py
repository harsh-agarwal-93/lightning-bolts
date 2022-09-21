import warnings
from pathlib import Path

import pytest
import torch
from pytorch_lightning import Trainer
from pytorch_lightning.utilities.warnings import PossibleUserWarning
from torch.utils.data import DataLoader

from pl_bolts.datasets import DummyDetectionDataset
from pl_bolts.models.detection import YOLO, FasterRCNN, RetinaNet, YOLOConfiguration
from pl_bolts.models.detection.faster_rcnn import create_fasterrcnn_backbone
from pl_bolts.models.detection.yolo.yolo_layers import _aligned_iou
from tests import TEST_ROOT


def _collate_fn(batch):
    return tuple(zip(*batch))


@torch.no_grad()
def test_fasterrcnn():
    model = FasterRCNN(pretrained=False, pretrained_backbone=False)

    image = torch.rand(1, 3, 224, 224)
    model(image)


def test_fasterrcnn_train(tmpdir):
    model = FasterRCNN(pretrained=False, pretrained_backbone=False)

    train_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    valid_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)

    trainer = Trainer(fast_dev_run=True, logger=False, enable_checkpointing=False, default_root_dir=tmpdir)
    trainer.fit(model, train_dataloaders=train_dl, val_dataloaders=valid_dl)


def test_fasterrcnn_bbone_train(tmpdir):
    torch.manual_seed(123)
    model = FasterRCNN(backbone="resnet18", fpn=True, pretrained_backbone=False)
    train_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    valid_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)

    trainer = Trainer(fast_dev_run=True, logger=False, enable_checkpointing=False, default_root_dir=tmpdir)
    trainer.fit(model, train_dl, valid_dl)


@torch.no_grad()
def test_retinanet():
    model = RetinaNet(pretrained=False)

    image = torch.rand(1, 3, 400, 400)
    model(image)


def test_retinanet_train(tmpdir):
    model = RetinaNet(pretrained=False)
    train_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    valid_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)

    trainer = Trainer(fast_dev_run=True, logger=False, enable_checkpointing=False, default_root_dir=tmpdir)
    trainer.fit(model, train_dataloaders=train_dl, val_dataloaders=valid_dl)


def test_retinanet_backbone_train(tmpdir):
    model = FasterRCNN(backbone="resnet18", fpn=True, pretrained_backbone=False, pretrained=False)
    trainer = Trainer(fast_dev_run=True, logger=False, enable_checkpointing=False, default_root_dir=tmpdir)
    train_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    valid_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    trainer.fit(model, train_dl, valid_dl)


def test_fasterrcnn_pyt_module_bbone_train(tmpdir):
    backbone = create_fasterrcnn_backbone(backbone="resnet18")
    model = FasterRCNN(backbone=backbone, fpn=True, pretrained_backbone=False, pretrained=False)
    train_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    valid_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)

    trainer = Trainer(fast_dev_run=True, default_root_dir=tmpdir)
    trainer.fit(model, train_dl, valid_dl)


@pytest.mark.parametrize("config", [("yolo"), ("yolo_giou")])
def test_yolo(config):
    config_path = Path(TEST_ROOT) / "data" / f"{config}.cfg"
    config = YOLOConfiguration(config_path)
    model = YOLO(config.get_network())

    image = torch.rand(1, 3, 256, 256)
    model(image)


@pytest.mark.parametrize(
    "cfg_name",
    [
        ("yolo"),
        ("yolo_giou"),
    ],
)
def test_yolo_train(tmpdir, cfg_name, catch_warnings):
    warnings.filterwarnings(
        "ignore",
        message=".*does not have many workers which may be a bottleneck.*",
        category=PossibleUserWarning,
    )

    config_path = Path(TEST_ROOT) / "data" / f"{cfg_name}.cfg"
    config = YOLOConfiguration(config_path)
    model = YOLO(config.get_network())

    train_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)
    valid_dl = DataLoader(DummyDetectionDataset(), collate_fn=_collate_fn)

    trainer = Trainer(fast_dev_run=True, default_root_dir=tmpdir, logger=False, max_epochs=10, accelerator="auto")
    trainer.fit(model, train_dataloaders=train_dl, val_dataloaders=valid_dl)


@pytest.mark.parametrize(
    "dims1, dims2, expected_ious",
    [
        (
            torch.tensor([[1.0, 1.0], [10.0, 1.0], [100.0, 10.0]]),
            torch.tensor([[1.0, 10.0], [2.0, 20.0]]),
            torch.tensor([[1.0 / 10.0, 1.0 / 40.0], [1.0 / 19.0, 2.0 / 48.0], [10.0 / 1000.0, 20.0 / 1020.0]]),
        )
    ],
)
def test_aligned_iou(dims1, dims2, expected_ious, catch_warnings):
    warnings.filterwarnings(
        "ignore",
        message=".*does not have many workers which may be a bottleneck.*",
        category=PossibleUserWarning,
    )

    torch.testing.assert_close(_aligned_iou(dims1, dims2), expected_ious)
