import logging
import time
import datetime
from typing import List, Optional
from PIL import Image

import numpy as np
import pandas as pd
import requests
import torch
import pytorch_lightning as pl

from zoobot.shared import save_predictions
# TODO these imports will work once galaxy_datasets PR merged
from galaxy_datasets.pytorch import galaxy_datamodule, galaxy_dataset

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# add retries on requests if we have flaky networks
# https://www.peterbe.com/plog/best-practice-with-retries-with-requests
def requests_retry_session(retries=3, backoff_factor=0.3):
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        # use the following to force a retry on the response status codes
        # status_forcelist=(104, 400, 404, 500, 502, 504)
    )
    adapter = HTTPAdapter(max_retries=retry)
    # should only have https scheme but let's be safe here
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# # override the get_galaxy_label package function for use in the prediction data loaders
# # essentially we don't need the labels in the prediction catalogues
# def predict_get_galaxy_label(galaxy, label_cols):
#     return []

# galaxy_dataset.get_galaxy_label = predict_get_galaxy_label


class PredictionGalaxyDataset(galaxy_dataset.GalaxyDataset):
  def __getitem__(self, idx):
      galaxy = self.catalog.iloc[idx]
      # load the data from the remote image URL
      url = galaxy['image_url']
      try:
          # Currently only supporting JPEG images
          # TODO: support other subject image formats (PNG etc)
          # Note: the model must be trained on similar image formats
          #
          # streaming the file as it is used (saves on memory)
          logging.debug('Downloading url: {}'.format(url))
          # use retries on requests if we have flaky networks
          response = requests_retry_session().get(url, stream=True)
          # ensure we raise other response errors like 404 and 500 etc
          # Note: we don't retry on errors that aren't in the `status_forcelist`, instead we fast fail!
          response.raise_for_status()
          url_mime_type = response.headers['content-type']
          # handle PNG images
          if url_mime_type == 'image/png':
              # use PIL image to read the png file buffer
              image = Image.open(response.raw)
          else: # but assume all other images are JPEG
              # HWC PIL image
              image = Image.fromarray(
                  galaxy_dataset.decode_jpeg(response.raw.read()))
      except Exception as e:
          # add some logging on the failed url
          logging.critical('Cannot load {}'.format(url))
          # and make sure we reraise the error
          raise e

      # avoid the label lookups as they aren't used in the prediction
      label = []

      # logging.info((image.shape, torch.max(image), image.dtype, label))  # always 0-255 uint8

      if self.transform:
          # a CHW tensor, which torchvision wants. May change to PIL image.
          image = self.transform(image)

      if self.target_transform:
          label = self.target_transform(label)

      # logging.info((image.shape, torch.max(image), image.dtype, label))  #  should be 0-1 float
      return image, label


class PredictionGalaxyDataModule(galaxy_datamodule.GalaxyDataModule):
    def setup(self, stage: Optional[str] = None):
        super().setup(stage)

        self.predict_dataset = PredictionGalaxyDataset(
            catalog=self.predict_catalog, label_cols=self.label_cols, transform=self.transform
        )


def predict(catalog: pd.DataFrame, model: pl.LightningModule, n_samples: int, label_cols: List, save_loc: str, datamodule_kwargs, trainer_kwargs):
    # extract the uniq image identifiers
    image_id_strs = list(catalog['subject_id'])

    predict_datamodule = PredictionGalaxyDataModule(
        label_cols=label_cols,
        predict_catalog=catalog,  # no need to specify the other catalogs
        # will use the default transforms unless overridden with datamodule_kwargs
        #
        **datamodule_kwargs  # e.g. batch_size, resize_size, crop_scale_bounds, etc.
    )

    # predict_datamodule = galaxy_datamodule.GalaxyDataModule(
    #     label_cols=label_cols,
    #     predict_catalog=catalog,  # no need to specify the other catalogs
    #     # will use the default transforms unless overridden with datamodule_kwargs
    #     #
    #     **datamodule_kwargs  # e.g. batch_size, resize_size, crop_scale_bounds, etc.
    # )
    # with this stage arg, will only use predict_catalog
    # crucial to specify the stage, or will error (as missing other catalogs)
    predict_datamodule.setup(stage='predict')

    # set up trainer (again)
    trainer = pl.Trainer(
        max_epochs=-1,  # does nothing in this context, suppresses warning
        **trainer_kwargs  # e.g. gpus
    )

    # from here, very similar to tensorflow version - could potentially refactor

    logging.info('Beginning predictions')
    start = datetime.datetime.fromtimestamp(time.time())
    logging.info('Starting at: {}'.format(start.strftime('%Y-%m-%d %H:%M:%S')))

    # derive the predictions
    predictions = trainer.predict(model, predict_datamodule)
    logging.info(len(predictions))

    # trainer.predict gives list of tensors, each tensor being predictions for a batch. Concat on axis 0.
    # range(n_samples) list comprehension repeats this, for dropout-permuted predictions. Stack to create new last axis.
    # final shape (n_galaxies, n_answers, n_samples)
    predictions = torch.stack([torch.concat(predictions, dim=0) for n in range(n_samples)], dim=2).numpy()

    logging.info('Predictions complete - {}'.format(predictions.shape))
    logging.info(f'Saving predictions to {save_loc}')

    if save_loc.endswith('.csv'):      # save as pandas df
        save_predictions.predictions_to_csv(predictions, image_id_strs, label_cols, save_loc)
    elif save_loc.endswith('.hdf5'):
        save_predictions.predictions_to_hdf5(predictions, image_id_strs, label_cols, save_loc)
    else:
        logging.warning('Save format of {} not recognised - assuming csv'.format(save_loc))
        save_predictions.predictions_to_csv(predictions, image_id_strs, label_cols, save_loc)

    logging.info(f'Predictions saved to {save_loc}')

    end = datetime.datetime.fromtimestamp(time.time())
    logging.info('Completed at: {}'.format(end.strftime('%Y-%m-%d %H:%M:%S')))
    logging.info('Time elapsed: {}'.format(end - start))


def predictions_to_expectation_of_answer(predictions: np.ndarray, question_indices: List[int], answer_index: int) -> np.ndarray:
    """
    Calculate expected vote fraction for some answer, from Zoobot dirichlet predictions

    https://en.wikipedia.org/wiki/Dirichlet_distribution
    See properties, moments, E[Xi]

    Args:
        predictions (np.ndarray): Dirichlet concentrations of shape (n_galaxies, n_answers)
        question_indices (List[int]): Start and end column index of the question's answers (e.g. [0, 2] for smooth or featured)
        answer_index (int): Column index of the answer (e.g. 0 for smooth)

    Returns:
        np.ndarray: expected value of Dirichlet variable for that answer (i.e. the fraction of volunteers giving that answer), shape (n_galaxies)
    """

    # could do in one line but might as well be explicit/clear
    alpha_all = predictions[:, question_indices[0]:question_indices[1]+1].sum(axis=1)
    alpha_i = predictions[:, answer_index]
    return alpha_i / alpha_all


def predictions_to_variance_of_answer(predictions: np.ndarray,  question_indices: List[int], answer_index: int) -> np.ndarray:
    """
    Calculate variance (uncertainty) on vote fraction for some answer, from Zoobot dirichlet predictions

    https://en.wikipedia.org/wiki/Dirichlet_distribution
    See properties, moments, Var[Xi]

    Args:
        predictions (np.ndarray): Dirichlet concentrations of shape (n_galaxies, n_answers)
        question_indices (List[int]): Start and end column index of the question's answers (e.g. [0, 2] for smooth or featured)
        answer_index (int): Column index of the answer (e.g. 0 for smooth)

    Returns:
        np.ndarray: Variance (uncertainty) of Dirichlet variable for that answer (i.e. the variance on the fraction of volunteers giving that answer), shape (n_galaxies)
    """
    alpha_all = predictions[:, question_indices[0]:question_indices[1]+1].sum(axis=1)
    alpha_i = predictions[:, answer_index]
    return alpha_i * (alpha_all - alpha_i) / (alpha_all**2 * (alpha_all + 1))


def test_predictions_to_expectation_of_answer():

    predictions = np.array([[8., 2., 1.5], [4., 5., 1.5]])

    smooth_or_featured_start_and_end_indices = [0, 2]
    smooth_or_featured_smooth_index = 0

    expectations = predictions_to_expectation_of_answer(
        predictions,
        smooth_or_featured_start_and_end_indices,
        smooth_or_featured_smooth_index
    )

    # first row should have higher expectation than second
    # assert np.allclose(expectations, [0.69565217, 0.38095238])
    print('Expectations: ', expectations)


def test_predictions_to_variance_of_answer():

    predictions = np.array([[8., 2., 1.5], [4., 5., 1.5]])

    smooth_or_featured_start_and_end_indices = [0, 2]
    smooth_or_featured_smooth_index = 0

    variances = predictions_to_variance_of_answer(
        predictions,
        smooth_or_featured_start_and_end_indices,
        smooth_or_featured_smooth_index
    )
    # first row should have smaller variance than second
    # assert np.allclose(variances, [0.01693762, 0.02050675])
    print('Variances: ', variances)


# TODO feel free to remove or make proper test
if __name__ == '__main__':

    test_predictions_to_expectation_of_answer()
    test_predictions_to_variance_of_answer()
