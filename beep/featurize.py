# Copyright 2019 Toyota Research Institute. All rights reserved.
"""
Module and scripts for generating descriptors (quantities listed
in cell_analysis.m) from cycle-level summary statistics.

Usage:
    featurize [INPUT_JSON]

Options:
    -h --help        Show this screen
    --version        Show version


The `featurize` script will generate features according to the methods
contained in beep.featurize.  It places output files corresponding to
features in `/data-share/features/`.

The input json must contain the following fields

* `file_list` - a list of processed cycler runs for which to generate features

The output json file will contain the following:

* `file_list` - a list of filenames corresponding to the locations of the features

Example:
```angular2
$ featurize '{"invalid_file_list": ["/data-share/renamed_cycler_files/FastCharge/FastCharge_0_CH33.csv",
    "/data-share/renamed_cycler_files/FastCharge/FastCharge_1_CH44.csv"],
    "file_list": ["/data-share/structure/FastCharge_2_CH29_structure.json"]}'
{"file_list": ["/data-share/features/FastCharge_2_CH29_full_model_features.json"]}
```
"""

import os
import json
import numpy as np
import pandas as pd
import math
from abc import ABCMeta, abstractmethod
from docopt import docopt
from monty.json import MSONable
from monty.serialization import loadfn, dumpfn
from scipy.stats import skew, kurtosis
from beep.collate import scrub_underscore_suffix, add_suffix_to_filename
from beep.utils import KinesisEvents
from beep.helpers import featurizer_helpers
from beep import logger, ENVIRONMENT, __version__
from beep.structure import get_protocol_parameters

s = {'service': 'DataAnalyzer'}


class BeepFeatures(MSONable, metaclass=ABCMeta):
    """
    Class corresponding to feature baseline feature object.
    Attributes:
        name (str): predictor object name.
        X (pandas.DataFrame): features in DataFrame format.
        metadata (dict): information about the conditions, data
            and code used to produce features
    """
    class_feature_name = 'Base'

    def __init__(self, name, X, metadata):
        self.name = name
        self.X = X
        self.metadata = metadata

    @classmethod
    def from_run(cls, input_filename, feature_dir, processed_cycler_run):
        """
        This method contains the workflow for the creation of the feature class
        Since the workflow should be the same for all of the feature classed this
        method should not be overridden in any of the derived classes. If the class
        can be created (feature generation succeeds, etc.) then the class is returned.
        Otherwise the return value is False
        Args:
            input_filename (str): path to the input data from processed cycler run
            feature_dir (str): path to the base directory for the feature sets.
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            (beep.featurize.BeepFeatures): class object for the feature set
        """
        if cls.validate_data(processed_cycler_run):
            output_filename = cls.get_feature_object_name_and_path(input_filename, feature_dir)
            feature_object = cls.features_from_processed_cycler_run(processed_cycler_run)
            metadata = cls.metadata_from_processed_cycler_run(processed_cycler_run)
            return cls(output_filename, feature_object, metadata)
        else:
            return False

    @classmethod
    @abstractmethod
    def validate_data(cls, processed_cycler_run):
        raise NotImplementedError

    @classmethod
    def get_feature_object_name_and_path(cls, input_path, feature_dir):
        """
        This function determines how to name the object for a specific feature class
        and creates the full path to save the object. This full path is also used as
        the feature name attribute
        Args:
            input_path (str): path to the input data from processed cycler run
            feature_dir (str): path to the base directory for the feature sets.
        Returns:
            str: the full path (including filename) to use for saving the feature
                object
        """
        new_filename = os.path.basename(input_path)
        new_filename = scrub_underscore_suffix(new_filename)

        # Append model_name along with "features" to demarcate
        # different models when saving the feature vectors.
        new_filename = add_suffix_to_filename(new_filename,
                                              "_features" + "_" + cls.class_feature_name)
        if not os.path.isdir(os.path.join(feature_dir, cls.class_feature_name)):
            os.makedirs(os.path.join(feature_dir, cls.class_feature_name))
        feature_path = os.path.join(feature_dir, cls.class_feature_name, new_filename)
        feature_path = os.path.abspath(feature_path)
        return feature_path

    @classmethod
    @abstractmethod
    def features_from_processed_cycler_run(cls, processed_cycler_run):
        raise NotImplementedError

    @classmethod
    def metadata_from_processed_cycler_run(cls, processed_cycler_run):
        metadata = {
            'barcode': processed_cycler_run.barcode,
            'protocol': processed_cycler_run.protocol,
            'channel_id': processed_cycler_run.channel_id
        }
        return metadata

    def as_dict(self):
        """
        Method for dictionary serialization
        Returns:
            dict: corresponding to dictionary for serialization
        """
        obj = {"@module": self.__class__.__module__,
               "@class": self.__class__.__name__,
               "name": self.name,
               "X": self.X.to_dict("list"),
               "metadata": self.metadata
               }
        return obj

    @classmethod
    def from_dict(cls, d):
        """MSONable deserialization method"""
        d['X'] = pd.DataFrame(d['X'])
        return cls(**d)


class DiagnosticCyclesFeatures(BeepFeatures):
    """
    Object corresponding to feature object. Includes constructors
    to create the features, object names and metadata attributes in the
    object
        name (str): predictor object name.
        X (pandas.DataFrame): features in DataFrame format.
        metadata (dict): information about the conditions, data
            and code used to produce features
    """
    # Class name for the feature object
    class_feature_name = 'DiagnosticCyclesFeatures'
    diagnostic_cycle_types = ['reset', 'hppc', 'rpt_0.2C', 'rpt_1C', 'rpt_2C']

    def __init__(self, name, X, metadata):
        """
        Args:
            name (str): predictor object name
            feature_object (pandas.DataFrame): features in DataFrame format.
            metadata (dict): information about the data and code used to produce features
        """
        super().__init__(name, X, metadata)
        self.name = name
        self.X = X
        self.metadata = metadata

    @classmethod
    def validate_data(cls, processed_cycler_run):
        """
        This function determines if the input data has the necessary attributes for
        creation of this feature class. It should test for all of the possible reasons
        that feature generation would fail for this particular input data.

        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            bool: True/False indication of ability to proceed with feature generation
        """
        conditions = []
        if not hasattr(processed_cycler_run, 'diagnostic_summary') & hasattr(processed_cycler_run, 'diagnostic_interpolated'):
            return False
        if processed_cycler_run.diagnostic_summary.empty:
            return False
        else:
            conditions.append(set(cls.diagnostic_cycle_types) ==
                              set(processed_cycler_run.diagnostic_summary.cycle_type.unique()))
            conditions.append(cls.check_relaxation_features_viable(processed_cycler_run))

        return all(conditions)

    @classmethod
    def check_relaxation_features_viable(cls, processed_cycler_run, n_soc_windows=8):
        """
        This function returns if it is viable to compute the relaxation features. Will return True if
        all the SOC windows for the HPPC are there for both the 1st and 2nd diagnostic cycles, and False
        if otherwise.

        Args:
            processed_cycler_run(beep.structure.ProcessedCyclerRun)
            n_soc_windows (int): threshold number of soc windows a cell must have


        Returns:
            (boolean): True if all SOC window available in both diagnostic cycles. False otherwise.
        """
        conditions_met = []
        # chooses the first and the second diagnostic cycle
        for hppc_chosen in [0, 1]:

            # Getting just the HPPC cycles
            hppc_diag_cycles = processed_cycler_run.diagnostic_interpolated[
                processed_cycler_run.diagnostic_interpolated.cycle_type == "hppc"]

            # Getting unique and ordered cycle index list for HPPC cycles, and choosing the hppc cycle
            hppc_cycle_list = list(set(hppc_diag_cycles.cycle_index))
            hppc_cycle_list.sort()

            # Getting unique and ordered Regular Step List (Non-unique identifier)
            reg_step_list = hppc_diag_cycles[hppc_diag_cycles.cycle_index == hppc_cycle_list[hppc_chosen]].step_index
            reg_step_list = list(set(reg_step_list))
            reg_step_list.sort()

            # The value of 1 for regular step corresponds to all of the relaxation curves in the hppc
            reg_step_relax = 1

            # Getting unique and ordered Step Counter List (unique identifier)
            step_count_list = hppc_diag_cycles[(hppc_diag_cycles.cycle_index == hppc_cycle_list[hppc_chosen]) &
                                               (hppc_diag_cycles.step_index == reg_step_list[
                                                   reg_step_relax])].step_index_counter
            step_count_list = list(set(step_count_list))
            step_count_list.sort()
            # The first one isn't a proper relaxation curve(comes out of CV) so we ignore it
            step_count_list = step_count_list[1:]
            conditions_met.append(len(step_count_list) >= n_soc_windows)

        return all(conditions_met)

    @classmethod
    def features_from_processed_cycler_run(cls, processed_cycler_run):
        """
        Generate features listed in early prediction manuscript, primarily related to the
        so called delta Q feature
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            pd.DataFrame: features indicative of degradation, derived from the input data
        """

        rpt_dQdV_features = cls.get_rpt_dQdV_features(processed_cycler_run, diag_ref=0, diag_nr=1,
                                                                           charge_y_n=1, rpt_type='rpt_0.2C')
        hppc_features = cls.get_hppc_features(processed_cycler_run)
        relaxation_features = cls.get_all_relaxation_features(processed_cycler_run)
        fast_charge_features = cls.get_fast_charge_features(processed_cycler_run,
                                                            diagnostic_cycle_type='rpt_0.2C',
                                                            cycle_comp_num=[0, 1], Q_seg=500)
        X = pd.concat([rpt_dQdV_features, hppc_features, relaxation_features, fast_charge_features], axis=1)

        return X

    @classmethod
    def metadata_from_processed_cycler_run(cls, processed_cycler_run):
        """
        Gather and generate information useful for filtering or subsetting the
        training feature objects for subsequent models
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            dict: information about the data source, conditions under which the run was
                performed, and other information useful for modeling and prediction
        """
        metadata = {
            'barcode': processed_cycler_run.barcode,
            'protocol': processed_cycler_run.protocol,
            'channel_id': processed_cycler_run.channel_id
        }
        return metadata

    @classmethod
    def get_rpt_dQdV_features(cls, processed_cycler_run, diag_ref=0, diag_nr=1, charge_y_n=1, rpt_type='rpt_0.2C',
                              plotting_y_n=0):
        """
        Generate features out of peakfits to rpt cycles

        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun)
            diag_ref (int): 0 (default) reference diagnostic cycle
            diag_nr (int): 1 (default) next diagnostic cycle occurence for a specific cycle_type.
            For example, if rpt_0.2C occurs at cycle_index = [2,42,147,250.. ],
            diag_ref = 0 would correspond to cycle_index = 2
            and diag_nr = 1 would correspond to cycle_index = 42

            charge_y_n (bool): 1 = charge, 0 = discharge
            rpt_type (str): type of rpt cycle.

        Returns:
             pd.DataFrame containing features based on gaussian fits to dQdV features in rpt cycles
        """

        if ((rpt_type == 'rpt_0.2C') and (charge_y_n == 1)):
            max_nr_peaks = 4

        elif ((rpt_type == 'rpt_0.2C') and (charge_y_n == 0)):
            max_nr_peaks = 4

        elif ((rpt_type == 'rpt_1C') and (charge_y_n == 1)):
            max_nr_peaks = 4

        elif ((rpt_type == 'rpt_1C') and (charge_y_n == 0)):
            max_nr_peaks = 3

        elif ((rpt_type == 'rpt_2C') and (charge_y_n == 1)):
            max_nr_peaks = 4

        elif ((rpt_type == 'rpt_2C') and (charge_y_n == 0)):
            max_nr_peaks = 3
        else:
            raise InputError("{} is not a valid rpt cycle".format(
                rpt_type))

        peak_fit_df_ref = featurizer_helpers.generate_dQdV_peak_fits(processed_cycler_run, diag_nr=diag_ref,
                                                                     charge_y_n=charge_y_n,
                                                                     rpt_type=rpt_type, plotting_y_n=plotting_y_n,
                                                                     max_nr_peaks=max_nr_peaks)
        peak_fit_df = featurizer_helpers.generate_dQdV_peak_fits(processed_cycler_run, diag_nr=diag_nr,
                                                                 charge_y_n=charge_y_n,
                                                                 rpt_type=rpt_type, plotting_y_n=plotting_y_n,
                                                                 max_nr_peaks=max_nr_peaks)

        return 1 + (peak_fit_df - peak_fit_df_ref) / peak_fit_df_ref

    @classmethod
    def get_hppc_features(cls, processed_cycler_run, diag_pos=1, soc_window=8):
        """
        This method calculates features based on voltage and resistance changes in hppc and rpt cycles
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun)
            diag_pos (int): diagnostic cycle occurence for a specific <diagnostic_cycle_type>. e.g.
            if rpt_0.2C, occurs at cycle_index = [2, 42, 147, 249 ...], <diag_pos>=2 would correspond to cycle_index 147
            soc_window (int): step index counter corresponding to the soc window of interest.

        Returns:
            dataframe of features based on voltage and resistance changes over a SOC window in hppc cycles
        """
        result = pd.DataFrame()

        # diffusion features
        diffusion_features = featurizer_helpers.get_diffusion_features(processed_cycler_run, diag_pos)

        # resistance features, but the resistance feature is a 9 by 6 dataframe, so we have to make it to 1 by 54
        hppc_r = pd.DataFrame()
        # the 9 by 6 dataframe
        df_dr = featurizer_helpers.get_dr_df(processed_cycler_run, diag_pos)
        # transform this dataframe to be 1 by 54
        columns = df_dr.columns
        for column in columns:
            for r in range(len(df_dr[column])):
                name = column + str(r)
                hppc_r[name] = [df_dr[column][r]]

        # the variance of ocv features
        hppc_ocv = featurizer_helpers.get_hppc_ocv(processed_cycler_run, diag_pos)

        # the v_diff features
        v_diff = featurizer_helpers.get_v_diff(processed_cycler_run, diag_pos, soc_window)

        # merge everything together as a final result dataframe
        result = pd.concat([hppc_r, hppc_ocv, v_diff, diffusion_features], axis=1)

        return result

    @classmethod
    def get_all_relaxation_features(cls, processed_cycler_run, soc_list = np.linspace(90, 10, 9, dtype='int'),
                                    percentage_list = [50, 80, 99]):
        """
        This function returns all of the relaxation features in a panda dataframe for a given processed cycler run.

        Args:
            processed_cycler_run(beep.structure.ProcessedCyclerRun): ProcessedCyclerRun object for the cell
            you want the diagnostic features for.
            soc_list (list): list of SOCs to evaluate time constants at
            percentage_list (list): time constants to evaluate

        Returns:
            @featureDf(pd.DataFrame): Columns are either SOC{#%}_degrad{#%} where the first #% is the
            SOC % and the second #% is the time taken at what % of the final voltage value of the relaxation
            curve. The other type is names var_{#%} which is the variance of the other features taken at a
            certain % of the final voltage value of the relaxation curve.
        """

        relax_feature_array = featurizer_helpers.get_relaxation_features(processed_cycler_run)
        col_names = []
        full_feature_array = []

        for i, percentage in enumerate(percentage_list):
            col_names.append("var_{0}%".format(percentage))
            full_feature_array.append(np.var(relax_feature_array[:, i]))

            for j, soc in enumerate(soc_list):
                col_names.append("SOC{0}%_degrad{1}%".format(soc, percentage))
                full_feature_array.append(relax_feature_array[j, i])

        return pd.DataFrame(dict(zip(col_names, full_feature_array)), index=[0])


    @classmethod
    def get_fast_charge_features(cls, processed_cycler_run, diagnostic_cycle_type, cycle_comp_num=[0, 1], Q_seg=500):
        """
        Generate features listed in early prediction manuscript using both diagnostic and regular cycles

        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun)
            diagnostic_cycle_type (str): Describes which cyle type is used for feature creation,
            options are: 'hppc', 'rpt_0.2C', 'rpt_1C', 'rpt_2C', 'reset'
            cycle_comp_num (list of two integers): contains numbers of compared cycles [0,1] for e.g.
            creates the features from the first and the second cycle of the cycle type
            Q_seg (int):  Number of cycles considered (first 500 for charging, the following 500 for discharging)


        Returns:
            X (pd.DataFrame): Dataframe containing the feature
        """

        diagnostic_interpolated = processed_cycler_run.diagnostic_interpolated

        X = pd.DataFrame(np.zeros((1, 42)))
        labels = []

        # Determine beginning and end of investigated cycle type
        index_pos_list = [i for i in range(len(diagnostic_interpolated.cycle_type))
                          if diagnostic_interpolated.cycle_type[i] == diagnostic_cycle_type]

        end_list = [index_pos_list[i] for i in range(len(index_pos_list) - 1)
                    if index_pos_list[i + 1] != index_pos_list[i] + 1]
        start_list = [index_pos_list[i] for i in range(1, len(index_pos_list))
                      if index_pos_list[i - 1] != index_pos_list[i] - 1]
        end_list.append(index_pos_list[-1])
        start_list.insert(0, index_pos_list[0])

        if diagnostic_interpolated.cycle_type[0] == diagnostic_cycle_type:
            start_list.insert(0, 1)
        if diagnostic_interpolated.cycle_type[len(diagnostic_interpolated.cycle_type) - 1] == diagnostic_cycle_type:
            end_list.append(len(diagnostic_interpolated.cycle_type) - 1)

        # Create features

        # Charging Capacity features

        # Discharging Capacity features
        Qd100_1 = diagnostic_interpolated.discharge_capacity[
                  start_list[cycle_comp_num[1]] + Q_seg + 1: start_list[cycle_comp_num[1]] + 2 * Q_seg]
        Qd10_1 = diagnostic_interpolated.discharge_capacity[
                 start_list[cycle_comp_num[0]] + Q_seg + 1: start_list[cycle_comp_num[0]] + 2 * Q_seg]
        QdDiff = [a_i - b_i for a_i, b_i in zip(Qd100_1, Qd10_1)]
        QdDiff = [elem for elem in QdDiff if (math.isnan(elem) == False)]

        X[7] = np.log10(np.absolute(np.var(QdDiff)))
        X[8] = np.log10(np.absolute(min(QdDiff)))
        X[9] = np.log10(np.absolute(np.mean(QdDiff)))
        X[10] = np.log10(np.absolute(skew(QdDiff)))
        X[11] = np.log10(np.absolute(kurtosis(QdDiff, fisher=False, bias=False)))
        X[12] = np.log10(np.sum(np.absolute(QdDiff)))
        X[13] = np.log10(np.sum(np.square(QdDiff)))

        # Charging Energy features

        # Discharging Energy features
        Ed100_1 = diagnostic_interpolated.discharge_energy[
                  start_list[cycle_comp_num[1]] + Q_seg + 1: start_list[cycle_comp_num[1]] + 2 * Q_seg]
        Ed10_1 = diagnostic_interpolated.discharge_energy[
                 start_list[cycle_comp_num[0]] + Q_seg + 1: start_list[cycle_comp_num[0]] + 2 * Q_seg]
        EdDiff = [a_i - b_i for a_i, b_i in zip(Ed100_1, Ed10_1)]
        EdDiff = [elem for elem in EdDiff if (math.isnan(elem) == False)]

        X[21] = np.log10(np.absolute(np.var(EdDiff)))
        X[22] = np.log10(np.absolute(min(EdDiff)))
        X[23] = np.log10(np.absolute(np.mean(EdDiff)))
        X[24] = np.log10(np.absolute(skew(EdDiff)))
        X[25] = np.log10(np.absolute(kurtosis(EdDiff, fisher=False, bias=False)))
        X[26] = np.log10(np.sum(np.absolute(EdDiff)))
        X[27] = np.log10(np.sum(np.square(EdDiff)))

        # Charging dQdV features

        # Discharging Capacity features
        dQdVd100_1 = diagnostic_interpolated.discharge_dQdV[
                     start_list[cycle_comp_num[1]] + Q_seg + 1: start_list[cycle_comp_num[1]] + 2 * Q_seg]
        dQdVd10_1 = diagnostic_interpolated.discharge_dQdV[
                    start_list[cycle_comp_num[0]] + Q_seg + 1: start_list[cycle_comp_num[0]] + 2 * Q_seg]
        dQdVdDiff = [a_i - b_i for a_i, b_i in zip(dQdVd100_1, dQdVd10_1)]
        dQdVdDiff = [elem for elem in dQdVdDiff if (math.isnan(elem) == False)]

        X[35] = np.log10(np.absolute(np.var(dQdVdDiff)))
        X[36] = np.log10(np.absolute(min(dQdVdDiff)))
        X[37] = np.log10(np.absolute(np.mean(dQdVdDiff)))
        X[38] = np.log10(np.absolute(skew(dQdVdDiff)))
        X[39] = np.log10(np.absolute(kurtosis(dQdVdDiff, fisher=False, bias=False)))
        X[40] = np.log10(np.sum(np.absolute(dQdVdDiff)))
        X[41] = np.log10(np.sum(np.square(dQdVdDiff)))

        operations = ['var', 'min', 'mean', 'skew', 'kurtosis', 'abs', 'square']
        quantities = ['charging_capacity', 'discharging_capacity', 'charging_energy', 'discharging_energy',
                      'charging_dQdV', 'discharging_dQdV']

        X.columns = [y + '_' + x for x in quantities for y in operations]
        return X


class DeltaQFastCharge(BeepFeatures):
    """
    Object corresponding to feature object. Includes constructors
    to create the features, object names and metadata attributes in the
    object
        name (str): predictor object name.
        X (pandas.DataFrame): features in DataFrame format.
        metadata (dict): information about the conditions, data
            and code used to produce features
    """
    # Class name for the feature object
    class_feature_name = 'DeltaQFastCharge'

    # Class variables
    init_pred_cycle = 10
    mid_pred_cycle = 91
    final_pred_cycle = 100

    def __init__(self, name, X, metadata):
        """
        Args:
            name (str): predictor object name
            feature_object (pandas.DataFrame): features in DataFrame format.
            metadata (dict): information about the data and code used to produce features
        """
        super().__init__(name, X, metadata)
        self.name = name
        self.X = X
        self.metadata = metadata

    @classmethod
    def validate_data(cls, processed_cycler_run):
        """
        This function determines if the input data has the necessary attributes for
        creation of this feature class. It should test for all of the possible reasons
        that feature generation would fail for this particular input data.

        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            bool: True/False indication of ability to proceed with feature generation
        """
        conditions = []
        if 'cycle_index' in processed_cycler_run.summary.columns:
            conditions.append(processed_cycler_run.summary.cycle_index.max() > cls.final_pred_cycle)
            conditions.append(processed_cycler_run.summary.cycle_index.min() <= cls.init_pred_cycle)
        else:
            conditions.append(len(processed_cycler_run.summary.index) > cls.final_pred_cycle)


        return all(conditions)

    @classmethod
    def features_from_processed_cycler_run(cls, processed_cycler_run):
        """
        Generate features listed in early prediction manuscript, primarily related to the
        so called delta Q feature
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            pd.DataFrame: features indicative of degradation, derived from the input data
        """

        assert cls.mid_pred_cycle > 10  # Sufficient cycles for analysis
        assert cls.final_pred_cycle > cls.mid_pred_cycle  # Must have final_pred_cycle > mid_pred_cycle
        ifinal = cls.final_pred_cycle - 1  # python indexing
        imid = cls.mid_pred_cycle - 1
        iini = cls.init_pred_cycle - 1
        summary = processed_cycler_run.summary
        cycles_to_average_over = 40  # For nominal capacity, use median discharge capacity of first n cycles

        if 'step_type' in processed_cycler_run.cycles_interpolated.columns:
            interpolated_df = processed_cycler_run.cycles_interpolated[
                processed_cycler_run.cycles_interpolated.step_type == 'discharge']
        else:
            interpolated_df = processed_cycler_run.cycles_interpolated
        X = pd.DataFrame(np.zeros((1, 20)))
        labels = []
        # Discharge capacity, cycle 2 = Q(n=2)
        X[0] = summary.discharge_capacity[1]
        labels.append("discharge_capacity_cycle_2")

        # Max discharge capacity - discharge capacity, cycle 2 = max_n(Q(n)) - Q(n=2)
        X[1] = max(summary.discharge_capacity[np.arange(cls.final_pred_cycle)] - summary.discharge_capacity[1])
        labels.append("max_discharge_capacity_difference")

        # Discharge capacity, cycle 100 = Q(n=100)
        X[2] = summary.discharge_capacity[ifinal]
        labels.append("discharge_capacity_cycle_100")

        # Feature representing time-temperature integral over cycles 2 to 100
        X[3] = np.nansum(summary.time_temperature_integrated[np.arange(cls.final_pred_cycle)])
        labels.append("integrated_time_temperature_cycles_1:100")

        # Mean of charge times of first 5 cycles
        X[4] = np.nanmean(summary.charge_duration[1:6])
        labels.append("charge_time_cycles_1:5")

        # Descriptors based on capacity loss between cycles 10 and 100.
        Qd_final = interpolated_df.discharge_capacity[interpolated_df.cycle_index == ifinal]
        Qd_10 = interpolated_df.discharge_capacity[interpolated_df.cycle_index == 9]

        Vd = interpolated_df.voltage[interpolated_df.cycle_index == iini]
        Qd_diff = Qd_final.values - Qd_10.values

        # If DeltaQ(V) is not an empty array, compute summary stats, else initialize with np.nan
        # Cells discharged rapidly over a narrow voltage window run into have no interpolated discharge steps
        if len(Qd_diff):
            X[5] = np.log10(np.abs(np.nanmin(Qd_diff)))  # Minimum
            X[6] = np.log10(np.abs(np.nanmean(Qd_diff)))  # Mean
            X[7] = np.log10(np.abs(np.nanvar(Qd_diff)))  # Variance
            X[8] = np.log10(np.abs(skew(Qd_diff)))  # Skewness
            X[9] = np.log10(np.abs(kurtosis(Qd_diff)))  # Kurtosis
            X[10] = np.log10(np.abs(Qd_diff[0]))  # First difference
        else:
            X[5:11] = np.nan

        labels.append("abs_min_discharge_capacity_difference_cycles_2:100")
        labels.append("abs_mean_discharge_capacity_difference_cycles_2:100")
        labels.append("abs_variance_discharge_capacity_difference_cycles_2:100")
        labels.append("abs_skew_discharge_capacity_difference_cycles_2:100")
        labels.append("abs_kurtosis_discharge_capacity_difference_cycles_2:100")
        labels.append("abs_first_discharge_capacity_difference_cycles_2:100")


        X[11] = max(summary.temperature_maximum[list(range(1, cls.final_pred_cycle))])  # Max T
        labels.append("max_temperature_cycles_1:100")

        X[12] = min(summary.temperature_minimum[list(range(1, cls.final_pred_cycle))])  # Min T
        labels.append("min_temperature_cycles_1:100")

        # Slope and intercept of linear fit to discharge capacity as a fn of cycle #, cycles 2 to 100

        X[13], X[14] = np.polyfit(
            list(range(1, cls.final_pred_cycle)),
            summary.discharge_capacity[list(range(1, cls.final_pred_cycle))], 1)

        labels.append("slope_discharge_capacity_cycle_number_2:100")
        labels.append("intercept_discharge_capacity_cycle_number_2:100")

        # Slope and intercept of linear fit to discharge capacity as a fn of cycle #, cycles 91 to 100
        X[15], X[16] = np.polyfit(
            list(range(imid, cls.final_pred_cycle)),
            summary.discharge_capacity[list(range(imid, cls.final_pred_cycle))], 1)
        labels.append("slope_discharge_capacity_cycle_number_91:100")
        labels.append("intercept_discharge_capacity_cycle_number_91:100")

        IR_trend = summary.dc_internal_resistance[list(range(1, cls.final_pred_cycle))]
        if any(v == 0 for v in IR_trend):
            IR_trend[IR_trend == 0] = np.nan

        # Internal resistance minimum
        X[17] = np.nanmin(IR_trend)
        labels.append("min_internal_resistance_cycles_2:100")

        # Internal resistance at cycle 2
        X[18] = summary.dc_internal_resistance[1]
        labels.append("internal_resistance_cycle_2")

        # Internal resistance at cycle 100 - cycle 2
        X[19] = summary.dc_internal_resistance[ifinal] - summary.dc_internal_resistance[1]
        labels.append("internal_resistance_difference_cycles_2:100")

        # Nominal capacity
        X[20] = np.median(summary.discharge_capacity.iloc[0:cycles_to_average_over])
        labels.append("nominal_capacity_by_median")

        X.columns = labels
        return X

    @classmethod
    def metadata_from_processed_cycler_run(cls, processed_cycler_run):
        """
        Gather and generate information useful for filtering or subsetting the
        training feature objects for subsequent models
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            dict: information about the data source, conditions under which the run was
                performed, and other information useful for modeling and prediction
        """
        metadata = {
            'barcode': processed_cycler_run.barcode,
            'protocol': processed_cycler_run.protocol,
            'channel_id': processed_cycler_run.channel_id
        }
        return metadata


class TrajectoryFastCharge(DeltaQFastCharge):
    """
    Object corresponding to cycle numbers at which the capacity drops below
     specific percentages of the initial capacity. Computed on the discharge
     portion of the regular fast charge cycles.

        name (str): predictor object name.
        X (pandas.DataFrame): features in DataFrame format.
        metadata (dict): information about the conditions, data
            and code used to produce features
    """
    # Class name for the feature object
    class_feature_name = 'TrajectoryFastCharge'

    def __init__(self, name, X, metadata):
        super().__init__(name, X, metadata)
        self.name = name
        self.X = X
        self.metadata = metadata

    @classmethod
    def features_from_processed_cycler_run(cls, processed_cycler_run):
        """
        Calculate the outcomes from the input data. In particular, the number of cycles
        where we expect to reach certain thresholds of capacity loss
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            pd.DataFrame: cycles at which capacity/energy degradation exceeds thresholds
        """
        y = processed_cycler_run.cycles_to_reach_set_capacities(
            thresh_max_cap=0.98, thresh_min_cap=0.78, interval_cap=0.03)
        return y

class DiagnosticProperties(DiagnosticCyclesFeatures):
    """
    This class stores fractional levels of degradation in discharge capacity and discharge energy
    relative to the first cycle at each diagnostic cycle, grouped by diagnostic cycle type.

        name (str): predictor object name.
        X (pandas.DataFrame): features in DataFrame format.
        metadata (dict): information about the conditions, data
            and code used to produce features
    """
    # Class name for the feature object
    class_feature_name = 'DiagnosticProperties'

    def __init__(self, name, X, metadata):
        super().__init__(name, X, metadata)
        self.name = name
        self.X = X
        self.metadata = metadata

    @classmethod
    def features_from_processed_cycler_run(cls, processed_cycler_run):
        """
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): data from cycler run
        Returns:
            pd.DataFrame: cycles at which capacity/energy degradation exceeds thresholds
        """

        quantities = ['discharge_energy', 'discharge_capacity']
        cycle_types = processed_cycler_run.diagnostic_summary.cycle_type.unique()
        X = pd.DataFrame()
        for quantity in quantities:
            for cycle_type in cycle_types:
                summary_diag_cycle_type = DiagnosticProperties.get_fractional_quantity_remaining(processed_cycler_run,
                                                                                                 quantity, cycle_type)
                summary_diag_cycle_type['cycle_type'] = cycle_type
                summary_diag_cycle_type['metric'] = quantity
                X = X.append(summary_diag_cycle_type)

        return X

    @staticmethod
    def get_fractional_quantity_remaining(processed_cycler_run, metric='discharge_energy',
                                          diagnostic_cycle_type='rpt_0.2C'):
        """
        Determine relative loss of <metric> in diagnostic_cycles of type <diagnostic_cycle_type> after 100 regular cycles

        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): information about cycler run
            metric (str): column name to use for measuring degradation
            diagnostic_cycle_type (str): the diagnostic cycle to use for computing the amount of degradation

        Returns:
            a dataframe with cycle_index and corresponding degradation relative to the first measured value
        """
        summary_diag_cycle_type = processed_cycler_run.diagnostic_summary[
            (processed_cycler_run.diagnostic_summary.cycle_type == diagnostic_cycle_type)
            & (processed_cycler_run.diagnostic_summary.cycle_index > 100)].reset_index()
        summary_diag_cycle_type = summary_diag_cycle_type[['cycle_index', metric]]
        summary_diag_cycle_type[metric] = summary_diag_cycle_type[metric] / \
                                          processed_cycler_run.diagnostic_summary[metric].iloc[0]
        summary_diag_cycle_type.columns = ['cycle_index', 'fractional_metric']
        return summary_diag_cycle_type


class DegradationPredictor(MSONable):
    """
    Object corresponding to feature matrix. Includes constructors
    to initialize the feature vectors.
    Attributes:
        name (str): predictor object name.
        X (pandas.DataFrame): data as records x features.
        y (pandas.DataFrame): targets.
        feature_labels (list): feature labels.
        predict_only (bool): True/False to specify predict/train mode.
        prediction_type (str): Type of regression - 'single' vs 'multi'.
        predicted_quantity (str): 'cycle' or 'capacity'.
        nominal_capacity (float):
    """

    def __init__(self, name, X, feature_labels=None, y=None, nominal_capacity=1.1,
                 predict_only=False, predicted_quantity="cycle", prediction_type="multi"):
        """
        Args:
            name (str): predictor object name
            X (pandas.DataFrame): features in DataFrame format.
            name (str): name of method for featurization.
            y (pandas.Dataframe or float): one or more outcomes.
            predict_only (bool): True/False to specify predict/train mode.
            predicted_quantity (str): 'cycle' or 'capacity'.
            prediction_type (str): Type of regression - 'single' vs 'multi'.
        """
        self.name = name
        self.X = X
        self.feature_labels = feature_labels
        self.predict_only = predict_only
        self.prediction_type = prediction_type
        self.predicted_quantity = predicted_quantity
        self.y = y
        self.nominal_capacity = nominal_capacity

    @classmethod
    def from_processed_cycler_run_file(cls, path, features_label='full_model', predict_only=False,
                                       predicted_quantity='cycle', prediction_type='multi',
                                       diagnostic_features=False):
        """
        Args:
            path (str): string corresponding to file path with ProcessedCyclerRun object.
            features_label (str): name of method for featurization.
            predict_only (bool): True/False to specify predict/train mode.
            predicted_quantity (str): 'cycle' or 'capacity'.
            prediction_type (str): Type of regression - 'single' vs 'multi'.
            diagnostic_features (bool): whether to compute diagnostic features.
        """
        processed_cycler_run = loadfn(path)

        if features_label == 'full_model':
            return cls.init_full_model(processed_cycler_run, predict_only=predict_only,
                                       predicted_quantity=predicted_quantity,
                                       diagnostic_features=diagnostic_features,
                                       prediction_type=prediction_type)
        else:
            raise NotImplementedError

    @classmethod
    def init_full_model(cls, processed_cycler_run, init_pred_cycle=10, mid_pred_cycle=91,
                        final_pred_cycle=100, predict_only=False, prediction_type='multi',
                        predicted_quantity="cycle", diagnostic_features=False):
        """
        Generate features listed in early prediction manuscript
        Args:
            processed_cycler_run (beep.structure.ProcessedCyclerRun): information about cycler run
            init_pred_cycle (int): index of initial cycle index used for predictions
            mid_pred_cycle (int): index of intermediate cycle index used for predictions
            final_pred_cycle (int): index of highest cycle index used for predictions
            predict_only (bool): whether or not to include cycler life in the object
            prediction_type (str): 'single': cycle life to reach 80% capacity.
                                   'multi': remaining capacity at fixed cycles
            predicted_quantity (str): quantity being predicted - cycles/capacity
            diagnostic_features (bool): whether or not to compute diagnostic features
        Returns:
            beep.featurize.DegradationPredictor: DegradationPredictor corresponding to the ProcessedCyclerRun file.
        """
        assert mid_pred_cycle > 10, 'Insufficient cycles for analysis'
        assert final_pred_cycle > mid_pred_cycle, 'Must have final_pred_cycle > mid_pred_cycle'
        ifinal = final_pred_cycle - 1  # python indexing
        imid = mid_pred_cycle - 1
        iini = init_pred_cycle - 1
        summary = processed_cycler_run.summary
        assert len(processed_cycler_run.summary) > final_pred_cycle, 'cycle count must exceed final_pred_cycle'
        cycles_to_average_over = 40  # For nominal capacity, use median discharge capacity of first n cycles

        # Features in "nature energy" set only use discharge portion of the cycle
        if 'step_type' in processed_cycler_run.cycles_interpolated.columns:
            interpolated_df = processed_cycler_run.cycles_interpolated[
                processed_cycler_run.cycles_interpolated.step_type == 'discharge']
        else:
            interpolated_df = processed_cycler_run.cycles_interpolated

        X = pd.DataFrame(np.zeros((1, 20)))
        labels = []
        # Discharge capacity, cycle 2 = Q(n=2)
        X[0] = summary.discharge_capacity[1]
        labels.append("discharge_capacity_cycle_2")

        # Max discharge capacity - discharge capacity, cycle 2 = max_n(Q(n)) - Q(n=2)
        X[1] = max(summary.discharge_capacity[np.arange(final_pred_cycle)] - summary.discharge_capacity[1])
        labels.append("max_discharge_capacity_difference")

        # Discharge capacity, cycle 100 = Q(n=100)
        X[2] = summary.discharge_capacity[ifinal]
        labels.append("discharge_capacity_cycle_100")

        # Feature representing time-temperature integral over cycles 2 to 100
        X[3] = np.nansum(summary.time_temperature_integrated[np.arange(final_pred_cycle)])
        labels.append("integrated_time_temperature_cycles_1:100")

        # Mean of charge times of first 5 cycles
        X[4] = np.nanmean(summary.charge_duration[1:6])
        labels.append("charge_time_cycles_1:5")

        # Descriptors based on capacity loss between cycles 10 and 100.
        Qd_final = interpolated_df.discharge_capacity[interpolated_df.cycle_index == ifinal]
        Qd_10 = interpolated_df.discharge_capacity[interpolated_df.cycle_index == 9]

        Vd = interpolated_df.voltage[interpolated_df.cycle_index == iini]
        Qd_diff = Qd_final.values - Qd_10.values

        X[5] = np.log10(np.abs(np.min(Qd_diff)))  # Minimum
        labels.append("abs_min_discharge_capacity_difference_cycles_2:100")

        X[6] = np.log10(np.abs(np.mean(Qd_diff)))  # Mean
        labels.append("abs_mean_discharge_capacity_difference_cycles_2:100")

        X[7] = np.log10(np.abs(np.var(Qd_diff)))  # Variance
        labels.append("abs_variance_discharge_capacity_difference_cycles_2:100")

        X[8] = np.log10(np.abs(skew(Qd_diff)))  # Skewness
        labels.append("abs_skew_discharge_capacity_difference_cycles_2:100")

        X[9] = np.log10(np.abs(kurtosis(Qd_diff)))  # Kurtosis
        labels.append("abs_kurtosis_discharge_capacity_difference_cycles_2:100")

        X[10] = np.log10(np.abs(Qd_diff[0]))  # First difference
        labels.append("abs_first_discharge_capacity_difference_cycles_2:100")

        X[11] = max(summary.temperature_maximum[list(range(1, final_pred_cycle))])  # Max T
        labels.append("max_temperature_cycles_1:100")

        X[12] = min(summary.temperature_minimum[list(range(1, final_pred_cycle))])  # Min T
        labels.append("min_temperature_cycles_1:100")

        # Slope and intercept of linear fit to discharge capacity as a fn of cycle #, cycles 2 to 100

        X[13], X[14] = np.polyfit(
            list(range(1, final_pred_cycle)),
            summary.discharge_capacity[list(range(1, final_pred_cycle))], 1)

        labels.append("slope_discharge_capacity_cycle_number_2:100")
        labels.append("intercept_discharge_capacity_cycle_number_2:100")

        # Slope and intercept of linear fit to discharge capacity as a fn of cycle #, cycles 91 to 100
        X[15], X[16] = np.polyfit(
            list(range(imid, final_pred_cycle)),
            summary.discharge_capacity[list(range(imid, final_pred_cycle))], 1)
        labels.append("slope_discharge_capacity_cycle_number_91:100")
        labels.append("intercept_discharge_capacity_cycle_number_91:100")

        IR_trend = summary.dc_internal_resistance[list(range(1, final_pred_cycle))]
        if any(v == 0 for v in IR_trend):
            IR_trend[IR_trend == 0] = np.nan

        # Internal resistance minimum
        X[17] = np.nanmin(IR_trend)
        labels.append("min_internal_resistance_cycles_2:100")

        # Internal resistance at cycle 2
        X[18] = summary.dc_internal_resistance[1]
        labels.append("internal_resistance_cycle_2")

        # Internal resistance at cycle 100 - cycle 2
        X[19] = summary.dc_internal_resistance[ifinal] - summary.dc_internal_resistance[1]
        labels.append("internal_resistance_difference_cycles_2:100")

        X.columns = labels
        if predict_only:
            y = None
        else:
            if prediction_type == 'single':
                y = processed_cycler_run.get_cycle_life()
            elif prediction_type == 'multi':
                if predicted_quantity == 'cycle':
                    y = processed_cycler_run.cycles_to_reach_set_capacities(
                        thresh_max_cap=0.98, thresh_min_cap=0.78, interval_cap=0.03)
                elif predicted_quantity == 'capacity':
                    y = processed_cycler_run.capacities_at_set_cycles()
                else:
                    raise NotImplementedError(
                        "{} predicted_quantity type not implemented".format(
                            predicted_quantity))
        nominal_capacity = np.median(summary.discharge_capacity.iloc[0:cycles_to_average_over])

        return cls('full_model', X, feature_labels=labels, y=y,
                   nominal_capacity=nominal_capacity, predict_only=predict_only,
                   prediction_type=prediction_type, predicted_quantity=predicted_quantity)

    def as_dict(self):
        """
        Method for dictionary serialization
        Returns:
            dict: corresponding to dictionary for serialization
        """
        obj = {"@module": self.__class__.__module__,
               "@class": self.__class__.__name__,
               "name": self.name,
               "X": self.X.to_dict("list"),
               "feature_labels": self.feature_labels,
               "predict_only": self.predict_only,
               "prediction_type": self.prediction_type,
               "nominal_capacity": self.nominal_capacity
               }
        if isinstance(self.y, pd.DataFrame):
            obj["y"] = self.y.to_dict("list")
        else:
            obj["y"] = self.y
        return obj

    @classmethod
    def from_dict(cls, d):
        """MSONable deserialization method"""
        d['X'] = pd.DataFrame(d['X'])
        return cls(**d)


def add_file_prefix_to_path(path, prefix):
    """
    Helper function to add file prefix to path.

    Args:
        path (str): full path to file.
        prefix (str): prefix for file.

    Returns:
        str: path with prefix appended to filename.

    """
    split_path = list(os.path.split(path))
    split_path[-1] = prefix + split_path[-1]
    return os.path.join(*split_path)


def process_file_list_from_json(file_list_json, processed_dir='data-share/features/'):
    """
    Function to take a json file containing processed cycler run file locations,
    extract features, dump the processed file into a predetermined directory,
    and return a jsonable dict of feature file locations.

    Args:
        file_list_json (str): json string or json filename corresponding
            to a dictionary with a file_list attribute,
            if this string ends with ".json", a json file is assumed
            and loaded, otherwise interpreted as a json string.
        processed_dir (str): location for processed cycler run output files
            to be placed.
        features_label (str): name of feature generation method.
        predict_only (bool): whether to calculate predictions or not.
        prediction_type (str): Single or multi-point predictions.
        predicted_quantity (str): quantity being predicted - cycle or capacity.

    Returns:
        str: json string of feature files (with key "file_list").

    """
    # Get file list and validity from json, if ends with .json,
    # assume it's a file, if not assume it's a json string
    if file_list_json.endswith(".json"):
        file_list_data = loadfn(file_list_json)
    else:
        file_list_data = json.loads(file_list_json)

    # Setup Events
    events = KinesisEvents(service='DataAnalyzer', mode=file_list_data['mode'])

    # Add root path to processed_dir
    processed_dir = os.path.join(os.environ.get("BEEP_PROCESSING_DIR", "/"),
                                 processed_dir)
    if not os.path.exists(processed_dir):
        os.makedirs(processed_dir)

    file_list = file_list_data['file_list']
    run_ids = file_list_data['run_list']
    processed_run_list = []
    processed_result_list = []
    processed_message_list = []
    processed_paths_list = []

    for path, run_id in zip(file_list, run_ids):
        logger.info('run_id=%s featurizing=%s', str(run_id), path, extra=s)
        processed_cycler_run = loadfn(path)

        featurizer_classes = [DeltaQFastCharge, TrajectoryFastCharge, DiagnosticCyclesFeatures, DiagnosticProperties]
        for featurizer_class in featurizer_classes:
            featurizer = featurizer_class.from_run(path, processed_dir, processed_cycler_run)
            if featurizer:
                dumpfn(featurizer, featurizer.name)
                processed_paths_list.append(featurizer.name)
                processed_run_list.append(run_id)
                processed_result_list.append("success")
                processed_message_list.append({'comment': '',
                                               'error': ''})
                logger.info('Successfully generated %s', featurizer.name, extra=s)
            else:
                processed_paths_list.append(path)
                processed_run_list.append(run_id)
                processed_result_list.append("incomplete")
                processed_message_list.append({'comment': 'Insufficient or incorrect data for featurization',
                                               'error': ''})
                logger.info('Unable to featurize %s', path, extra=s)

    output_data = {"file_list": processed_paths_list,
                   "run_list": processed_run_list,
                   "result_list": processed_result_list,
                   "message_list": processed_message_list
                   }

    events.put_analyzing_event(output_data, 'featurizing', 'complete')
    # Return jsonable file list
    return json.dumps(output_data)


def main():
    """
    Main function of this module, takes in arguments of an input
    and output filename corresponding to structured cycler run data
    and creates a predictor object output for analysis/ML processing

    Returns:
        None

    """
    # Parse args and construct initial cycler run
    logger.info('starting', extra=s)
    logger.info('Running version=%s', __version__, extra=s)
    try:
        args = docopt(__doc__)
        input_json = args['INPUT_JSON']
        print(process_file_list_from_json(input_json), end="")
    except Exception as e:
        logger.error(str(e), extra=s)
        raise e
    logger.info('finish', extra=s)
    return None


if __name__ == "__main__":
    main()
