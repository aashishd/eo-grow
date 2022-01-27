"""
Task definitions for prediction
"""
import abc
from typing import List, Optional, Tuple, Callable

import numpy as np
import joblib

from sentinelhub import SHConfig
from eolearn.core import EOPatch, EOTask, execute_with_mp_lock, get_filesystem

from ..utils.types import Feature


class BasePredictionTask(EOTask, metaclass=abc.ABCMeta):
    """Base predictions task streamlining data preprocessing before prediction"""

    def __init__(
        self,
        *,
        model_folder: str,
        model_filename: str,
        input_features: List[Feature],
        mask_feature: Feature,
        output_feature: Feature,
        mp_lock: bool,
        sh_config: SHConfig,
    ):
        """
        :param model_folder: Path to the folder where model is stored
        :param model_filename: Name of file containing the model
        :param input_features: List of features containing input for the model, which are concatenated in given order
        :param mask_feature: Mask specifying which points are to be predicted
        :param output_feature: Feature into which predictions are saved
        :param mp_lock: If predictions should be executed with a multiprocessing lock
        :param sh_config: SentinelHub config
        """

        self.model_folder = model_folder
        self.model_filename = model_filename
        self._model = None

        self.input_features = input_features
        self.mask_feature = mask_feature
        self.output_feature = output_feature

        self.mp_lock = mp_lock
        self.sh_config = sh_config

    def process_data(self, eopatch: EOPatch, mask: np.ndarray) -> List[np.ndarray]:
        """Masks and reshapes data into a form suitable for the model"""
        all_features = []
        for ftype, fname in self.input_features:
            array = eopatch[ftype, fname]

            if ftype.is_timeless():
                all_features.append(array[mask, :])
            else:
                array = array[:, mask, :]
                time, pixels, depth = array.shape
                array = np.moveaxis(array, 0, 1)
                all_features.append(array.reshape(pixels, time * depth))

        return np.concatenate(all_features, axis=-1)

    @property
    def model(self):
        """Implements lazy loading that gets around file-system issues"""
        if self._model is None:
            file_system = get_filesystem(self.model_folder, config=self.sh_config)
            with file_system.openbin(self.model_filename, "r") as file_handle:
                self._model = joblib.load(file_handle)
        return self._model

    def apply_predictor(
        self, predictor: Callable, processed_features: np.ndarray, return_on_empty: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """Helper function that applies the predictor according to the mp_lock settings"""
        if processed_features.shape[0] == 0 and return_on_empty is not None:
            return return_on_empty

        if self.mp_lock:
            return execute_with_mp_lock(predictor, processed_features)
        return predictor(processed_features)

    @abc.abstractmethod
    def add_predictions(self, eopatch: EOPatch, processed_features: np.ndarray, mask: np.ndarray) -> EOPatch:
        """Runs the model prediction on given features and adds them to the eopatch. Must reverse mask beforehand."""

    @staticmethod
    def transform_to_feature_form(predictions: np.ndarray, mask: np.ndarray, no_value=0) -> np.ndarray:
        """Transforms an array of predictions into an EOPatch suitable array, making sure to reverse the masking"""
        full_predictions = np.full((*mask.shape, predictions.shape[-1]), dtype=predictions.dtype, fill_value=no_value)
        full_predictions[mask, :] = predictions
        return full_predictions

    def execute(self, eopatch: EOPatch) -> EOPatch:
        """Run model on input features and save predictions to eopatch"""

        some_feature = self.input_features[0]
        mask_size = eopatch.get_spatial_dimension(*some_feature)
        mask = np.squeeze(eopatch[self.mask_feature], axis=-1) if self.mask_feature else np.ones(mask_size)
        mask = mask.astype(bool)

        preprocessed_features = self.process_data(eopatch, mask)
        eopatch = self.add_predictions(eopatch, preprocessed_features, mask)
        return eopatch


class ClassificationPredictionTask(BasePredictionTask):
    """Uses a classification model to produce predictions for given input features"""

    def __init__(
        self,
        *,
        label_encoder_filename: str,
        output_probability_feature: Optional[Feature] = None,
        **kwargs,
    ):
        """
        :param label_encoder_filename: Name of file containing the label encoder with which to decode predictions
        :param output_probability_feature: If specified saves pseudo-probabilities into given feature.
        :param kwargs: Parameters of `BasePredictionTask`
        """
        self.label_encoder_filename = label_encoder_filename
        self._label_encoder = None
        self.output_probability_feature = output_probability_feature
        super().__init__(**kwargs)

    @property
    def label_encoder(self):
        """Implements lazy loading that gets around file-system issues"""
        if self._label_encoder is None and self.label_encoder_filename is not None:
            file_system = get_filesystem(self.model_folder, config=self.sh_config)
            with file_system.openbin(self.label_encoder_filename, "r") as file_handle:
                self._label_encoder = joblib.load(file_handle)
        return self._label_encoder

    def add_predictions(self, eopatch: EOPatch, processed_features: np.ndarray, mask: np.ndarray) -> EOPatch:
        """Runs the model prediction on given features and adds them to the eopatch

        If specified also adds probability scores and uses a label encoder.
        """

        predictions = self.apply_predictor(self.model.predict, processed_features, np.zeros((0,), dtype=np.uint8))
        predictions = predictions[..., np.newaxis]
        if self.label_encoder is not None:
            predictions = self.label_encoder.inverse_transform(predictions)
        eopatch[self.output_feature] = self.transform_to_feature_form(predictions, mask)

        if self.output_probability_feature is not None:
            probabilities = self.apply_predictor(self.model.predict_proba, processed_features)
            eopatch[self.output_probability_feature] = self.transform_to_feature_form(probabilities, mask)

        return eopatch


class RegressionPredictionTask(BasePredictionTask):
    """Computes values and scores given an input model and eopatch feature name"""

    def __init__(
        self,
        *,
        clip_predictions: Optional[Tuple[float, float]],
        **kwargs,
    ):
        """
        :param clip_predictions: If given the task also clips predictions to the specified interval.
        :param kwargs: Parameters of `BasePredictionTask`
        """
        self.clip_predictions = clip_predictions
        super().__init__(**kwargs)

    def add_predictions(self, eopatch: EOPatch, processed_features: np.ndarray, mask: np.ndarray) -> EOPatch:
        """Runs the model prediction on given features and adds them to the eopatch. Must reverse mask beforehand."""

        predictions = self.apply_predictor(self.model.predict, processed_features, np.zeros((0,), dtype=np.float32))
        predictions = predictions[..., np.newaxis]
        if self.clip_predictions is not None:
            predictions = predictions.clip(*self.clip_predictions)
        eopatch[self.output_feature] = self.transform_to_feature_form(predictions, mask)

        return eopatch
