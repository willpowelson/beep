# Copyright 2019 Toyota Research Institute. All rights reserved.
"""
Module and scripts for assembling a ML model training dataset

Options:
    -h --help        Show this screen
    --fit            <true_or_false>  [default: False] Fit model
    --version        Show version


The `dataset` script will assemble a BeepDataset object for ML model training
It stores its outputs in `/data-share/datasets/`


"""
from __future__ import division
import os
import pandas as pd
import numpy as np
from monty.json import MSONable
from monty.serialization import loadfn, dumpfn
from functools import reduce
from beep.collate import scrub_underscore_suffix, add_suffix_to_filename
from beep.structure import get_protocol_parameters
from beep.featurize import (
    RPTdQdVFeatures, HPPCResistanceVoltageFeatures,
    HPPCRelaxationFeatures, DiagnosticProperties,
    DiagnosticSummaryStats, DeltaQFastCharge,
    TrajectoryFastCharge
)
from sklearn.model_selection import train_test_split

FEATURE_HYPERPARAMS = loadfn(
    os.path.join(MODULE_DIR, "features/feature_hyperparameters.yaml")
)

assert all("_" not in name for name in DEFAULT_MODEL_PROJECTS)

FEATURIZER_CLASSES = [RPTdQdVFeatures, HPPCResistanceVoltageFeatures,
                      HPPCRelaxationFeatures, DiagnosticSummaryStats,
                      DiagnosticProperties]


class BeepDataset(MSONable, metaclass=ABCMeta):
    """
    Class corresponding to a training dataset assembled from BeepFeatures objects

    Attributes:

    """

    def __init__(self, name, data, metadata, filenames, feature_sets, dataset_dir):
        """
        :param name (str): name of the dataset
        :param data (pd.DataFrame): dataframe composed of different features concatenated column-wise and
        different runs concatenated row-wise
        :param metadata (list): list of metadata dicts for the different feature objects
        :param filenames (list): list of filenames that have atleast one of the feature objects
        :param feature_sets (dict): list of feature sets that were merged. This could be used
        to group sets of features for techniques like grouped/hierarchical lasso etc
        :param dataset_dir (str): path to store serialized dataset
        """
        self.name = name
        self.data = data
        self.metadata = metadata
        self.filenames = filenames
        self.feature_sets = feature_sets
        self.dataset_dir = dataset_dir
        self.train_cells_parameter_dict = {}
        self.test_cells_parameter_dict = {}
        self.X_train = None
        self.X_test = None
        self.y_train = None
        self.y_test = None

    def as_dict(self):
        """
        Method for dictionary serialization
        Returns:
            dict: corresponding to dictionary for serialization
        """
        obj = {
            "@module": self.__class__.__module__,
            "@class": self.__class__.__name__,
            "name": self.name,
            "data": self.data.to_dict("list"),
            "metadata": self.metadata,
            "filenames": self.filenames,
            "feature_sets": self.feature_sets
        }
        return obj

    @classmethod
    def from_dict(cls, d):
        """MSONable deserialization method"""
        d["data"] = pd.DataFrame(d["data"])
        return cls(**d)

    @classmethod
    def from_features(cls, name, project_list=['PreDiag'], feature_class_list=FEATURIZER_CLASSES,
                      feature_dir="data-share/features/", dataset_dir="data-share/datasets")
        """
        Method to assemble a dataset from a list of BeepFeatures objects generated for one or more projects. 

        :param project_list: list of projects from which training data will be assembled
        :param feature_class_list: list of features to be concatenated row-wise
        :param feature_dir: Root directory for features. Assumes that all objects belonging to a feature class
        are stored in a folder <feature_dir>/<MyFeatureSet.class_feature_name>
        :param dataset_dir:

        :return: beep.BeepDataset
        """
        feature_df_list = []
        metadata = []
        feature_sets = {}
        for feature_class in feature_class_list:
            feature_df = pd.DataFrame()
            for project in project_list:
                feature_path = os.path.join(feature_dir, feature_class.class_feature_name)
                feature_jsons = [f for f in os.listdir(feature_path) if
                                 (os.path.isfile(os.path.join(feature_path, f)) and
                                  f.startswith(project))]
                for feature_json in feature_jsons:
                    obj = loadfn(feature_json)
                    df = obj.X
                    df['file'] = obj.metadata['protocol']
                    feature_df = pd.concat([feature_df, df]).reset_index(drop=True)
                    ## TODO: Need some logic for ensuring that features of a given class being concatenated
                    ## row-wise have the same metadata dict

            feature_df_list.append(feature_df)
            feature_sets[feature_class] = list(feature_df.columns)

        df = reduce(lambda x, y: pd.merge(x, y, on='file', how='inner'), feature_df_list)
        return cls(name, df, metadata, df.file.unique(), feature_sets, dataset_dir)

    @classmethod
    def from_processed_cycler_runs(cls, project_list, processed_run_list=None,
                                   feature_class_list=FEATURIZER_CLASSES,
                                   metadata_list=None, processed_dir="data-share/structure/")
        """
        Method to assemble a dataset directly from a list of ProcessedCyclerRun objects
        Expected folder structure:

        :param project_list: list of projects to featurize and combine as a training dataset
        :param processed_run_list: list of paths to specific ProcessedCyclerRun objects to be featurized. 
        If provided, this will over-ride project based looping.
        :param feature_class_list: list of featurizers to invoke on the structured cycler files.
        :param feature_dir: location to store serialized feature jsons
        :param dataset_dir: location to store dataset
        :return:
        """
        feature_df_list = [pd.DataFrame()] * len(feature_class_list)
        feature_sets = {}
        failed_featurizations = pd.DataFrame(columns=['filename', ])

        # Check if metadata is present, and if yes, check if the dictionary keys are right
        if metadata_list is None:
            print('No metadata specified for feature generation. Assuming defaults and proceeding.')
            for idx, feature_class in enumerate(feature_class_list):
                if feature_class.class_feature_name in FEATURE_HYPERPARAMS.keys():
                    metadata_list[idx] = FEATURE_HYPERPARAMS[feature_class.class_feature_name]
                else:
                    metadata_list[idx] = None
        else:
            for idx, feature_class in enumerate(feature_class_list):
                if metadata_list[idx] is None:
                    if feature_class.class_feature_name in FEATURE_HYPERPARAMS.keys():
                        metadata_list[idx] = FEATURE_HYPERPARAMS[feature_class.class_feature_name]
                    else:
                        metadata_list[idx] = None
                elif set(FEATURE_HYPERPARAMS[feature_class.class_feature_name.keys()]) != \
                        set(metadata_list[idx].keys()):
                    raise ValueError('Invalid hyperparameter dictionary')

        if processed_run_list is None:
            processed_run_list = [f
                                  for f in os.listdir(processed_dir)
                                  for project in project_list
                                  if (os.path.isfile(os.path.join(processed_dir, f)) and
                                      f.startswith(project))]

        for processed_json in processed_run_list:
            path = os.path.join(processed_dir, processed_json)
            processed_cycler_run = loadfn(path)
            for idx, feature_class in enumerate(feature_class_list):
                obj = feature_class.from_run(
                    path, '/data-share/features/', processed_cycler_run, metadata_list[idx])
                df = obj.X
                df['file'] = obj.protocol
                feature_df_list[idx] = pd.concat([feature_df_list[idx], df])

            for idx, feature_class in enumerate(feature_class_list):
                feature_sets[feature_class] = list(feature_df_list[idx].columns)

        df = reduce(lambda x, y: pd.merge(x, y, on='file', how='inner'), feature_df_list)
        return cls(df, metadata_list, df.file.unique(), feature_sets)

    def generate_train_test_split(self, predictors=None, outcomes=None,
                                  split_by_cell=True, test_size=0.4, seed=123,
                                  parameters_path="data-share/raw/parameters"):
        """
        Method that subsets self.data into training and test datasets.
        Requires specification of columns to use as predictors and outcomes

        :param predictors (list): list of columns to use as predictors
        :param outcomes (list): list of columns to use as outcomes
        :param split_by_cell (bool): If True, train-test split on a per-run basis (self.filenames)
        Useful when there are multiple data-points per cell to avoid data-leaks between train and test data.
        :param seed (int): seed to ensure reproducible 'randomization'
        :param parameters_path (str): Root directory storing project parameter files.
        Assumes that parameter files begin with project name
        :return: X_train, X_test, y_train, y_test
        """

        if predictors is None:
            raise ValueError('Specify one or more predictor columns')

        if outcomes is None:
            raise ValueError('Specify one or more outcomes')

        np.random.seed(seed)
        if split_by_cell:
            test_cells = np.random.choice(self.filenames, int(len(self.filenames) * test_size))
            train_cells = [x for x in self.filenames if x not in test_cells]

            self.X_train = self.data.loc[self.data.file.isin(train_cells), predictors]
            self.X_test = self.data.loc[self.data.file.isin(test_cells), predictors]

            self.y_train = self.data.loc[self.data.file.isin(train_cells), outcomes]
            self.y_test = self.data.loc[self.data.file.isin(test_cells), outcomes]
        else:
            self.X_train, self.X_test, self.y_train, self.y_test = \
                train_test_split(self.data[predictors], self.data[outcomes], test_size, random_state=seed)

        if parameters_path is not None:
            self.train_cells_parameter_dict = get_parameter_dict(train_cells, parameters_path)
            self.test_cells_parameter_dict = get_parameter_dict(test_cells, parameters_path)

        return self.X_train, self.X_test, self.y_train, self.y_test

    def serialize(self):
        """
        Method to serialize dataset
        Args:
            processed_dir (dict): target directory.

        Returns: Path to serialized dataset

        """
        if not os.path.exists(self.dataset_dir):
            os.makedirs(self.dataset_dir)
        dumpfn(self, os.path.join(self.dataset_dir, self.name))
        return self.dataset_dir


def get_parameter_dict(file_list, parameters_path):
    """
    Helper function to generate a dictionary with
    :param file_list: List of filenames from self.filenames
    :param parameters_path: Root directory storing project parameter files.
    :return: Dictionary with file_list as keys, and corresponding dictionary of protocol parameters
    as values
    """
    d = {} #dict allows combining two different project parameter sets into the same structure
    for file in file_list:
        param_row, _ = get_protocol_parameters(file, parameters_path)
        d[file] = param_row.to_dict('records')[0] #to_dict('records') returns a list.
    return d


