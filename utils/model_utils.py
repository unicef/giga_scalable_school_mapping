import os
import yaml
import operator
import pandas as pd
import geopandas as gpd
import rasterio as rio

import numpy as np
from utils import eval_utils
from utils import data_utils
from utils import config_utils

import logging

logging.basicConfig(level=logging.INFO)
SEED = 42


def load_data(
    config: dict,
    iso_code: str = None,
    attributes: list = ["rurban"],
    in_dir: str = "clean",
    out_dir: str = "train",
    verbose: bool = True,
) -> gpd.GeoDataFrame:
    """
    Load and process geospatial data for training/validation/testing.

    Args:
        config (Dict[str, Any]): Configuration dictionary with the following keys:
            - vectors_dir (str): Directory containing vector data.
            - project (str): Project name.
            - iso_codes (List[str]): List of ISO country codes.
            - name (str, optional): Name for the dataset. Defaults to the first ISO code if not provided.
            - test_size (float): Proportion of the data to include in the test split.
            - pos_class (str): Label for the positive class.
            - neg_class (str): Label for the negative class.
        attributes (List[str], optional): List of attributes to use. Defaults to ["rurban"].
        in_dir (str, optional): Input directory name. Defaults to "clean".
        out_dir (str, optional): Output directory name. Defaults to "train".
        verbose (bool, optional): If True, prints detailed logs. Defaults to True.

    Returns:
        gpd.GeoDataFrame: Processed geospatial data.
    """
    # Construct the directory path for vectors
    vector_dir = os.path.join(os.getcwd(), config["vectors_dir"], config["project"])
    if iso_code:
        config["iso_codes"] = [iso_code]
    iso_codes = config["iso_codes"]

    # Determine the name for the dataset
    name = config["name"] if "name" in config else iso_codes[0]

    # Construct the file path for the output GeoJSON file
    out_file = os.path.join(
        os.getcwd(), vector_dir, out_dir, f"{name}_{out_dir}.geojson"
    )

    if len(iso_codes) > 1:
        data = []
        for iso_code in data_utils.create_progress_bar(iso_codes):
            sub_out_file = os.path.join(
                os.getcwd(), vector_dir, out_dir, f"{iso_code}_{out_dir}.geojson"
            )
            subdata = gpd.read_file(sub_out_file)
            data.append(subdata)
        data = pd.concat(data)
        print_stats(data, attributes, config["test_size"])
        data.to_file(out_file, driver="GeoJSON")
        return data

    # Check if the output file already exists
    if os.path.exists(out_file):
        data = gpd.read_file(out_file)
        if verbose:
            # logging.info(f"Loading existing file: {out_file}")
            print_stats(data, attributes, config["test_size"])
        return data

    data = []
    data_utils.makedir(os.path.dirname(out_file))

    # Iterate over the ISO codes to read and process data
    for iso_code in iso_codes:
        # Construct file paths for positive and negative classes
        positive_file = os.path.join(
            vector_dir,
            config["pos_class"],
            in_dir,
            f"{iso_code}_{in_dir}.geojson",
        )
        negative_file = os.path.join(
            vector_dir, config["neg_class"], in_dir, f"{iso_code}_{in_dir}.geojson"
        )

        # Read positive class data
        positive = gpd.read_file(positive_file)
        positive["class"] = config["pos_class"]
        if "validated" in positive.columns:
            positive = positive[positive["validated"] == 0]

        # Read negative class data
        negative = gpd.read_file(negative_file)
        negative["class"] = config["neg_class"]
        if "validated" in negative.columns:
            negative = negative[negative["validated"] == 0]

        # Combine positive and negative data
        data.append(pd.concat([positive, negative]))

    # Concatenate all the data into a single GeoDataFrame
    data = gpd.GeoDataFrame(pd.concat(data))

    # Filter dataset by the clean column
    data = data[data["clean"] == 0]

    # Apply rural-urban classification
    data = get_rurban_classification(config, data)

    # Split the data into training, validation, and testing sets
    data = train_val_test_split(
        data, test_size=config["test_size"], attributes=attributes, verbose=verbose
    )

    # Save the processed data to a GeoJSON file
    data.to_file(out_file, driver="GeoJSON")
    if verbose:
        logging.info(f"Generating file: {out_file}")
        print_stats(data, attributes, test_size=config["test_size"])

    return data


def print_stats(data: gpd.GeoDataFrame, attributes: list, test_size: float) -> None:
    """
    Print and log various statistics about the dataset.

    Args:
        data (gpd.GeoDataFrame): The data to analyze.
        attributes (list): List of attributes to include in the analysis.
        test_size (float): Proportion of the data to include in the test split.

    Returns:
        None
    """
    # Calculate total size of the dataset
    total_size = len(data)
    # Calculate the absolute size of the test dataset
    test_size = int((total_size * test_size))
    # Include 'class' attribute in the attributes list
    attributes = attributes + ["class"]

    # Calculate value counts for each combination of attributes and class
    value_counts = data.groupby(attributes)[attributes[-1]].value_counts()
    value_counts = pd.DataFrame(value_counts).reset_index()
    value_counts["percentage"] = value_counts["count"] / total_size
    logging.info(f"\n{value_counts}")

    # Calculate size and percentage for each combination of
    # attributes, class, and dataset (train/val/test)
    subcounts = pd.DataFrame(
        data.groupby(attributes + ["dataset"]).size().reset_index()
    )
    subcounts.columns = attributes + ["dataset", "count"]
    subcounts["percentage"] = (
        subcounts[subcounts.dataset != "train"]["count"] / test_size
    )
    subcounts = subcounts.set_index(attributes + ["dataset"])
    logging.info(f"\n{subcounts.to_string()}")

    # If there are multiple ISO codes in the data, calculate additional statistics
    if len(data.iso.unique()) > 1:
        # Size and percentage for each combination of ISO, dataset, and class
        subcounts = pd.DataFrame(
            data.groupby(["iso", "dataset", "class"]).size().reset_index()
        )
        subcounts.columns = ["iso", "dataset", "class", "count"]
        subcounts = subcounts.set_index(["iso", "dataset", "class"])
        logging.info(f"\n{subcounts.to_string()}")

        # Size and percentage for each combination of ISO and dataset
        subcounts = pd.DataFrame(data.groupby(["iso", "dataset"]).size().reset_index())
        subcounts.columns = ["iso", "dataset", "count"]
        subcounts = subcounts.set_index(["iso", "dataset"])
        logging.info(f"\n{subcounts.to_string()}")

    # Size and percentage for each combination of dataset and class
    subcounts = pd.DataFrame(data.groupby(["dataset", "class"]).size().reset_index())
    subcounts.columns = ["dataset", "class", "count"]
    subcounts["percentage"] = subcounts["count"] / total_size
    subcounts = subcounts.set_index(["dataset", "class"])

    # Log value counts and normalized value counts for the 'dataset' column
    logging.info(f"\n{subcounts.to_string()}")
    logging.info(f"\n{data.dataset.value_counts()}")
    logging.info(f"\n{data.dataset.value_counts(normalize=True)}")

    # Log value counts and normalized value counts for each attribute
    for attribute in attributes:
        if attribute != "iso":
            logging.info(f"\n{data[attribute].value_counts()}")
            logging.info(f"\n{data[attribute].value_counts(normalize=True)}")


def get_rurban_classification(config: dict, data: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Classify regions as urban or rural based on GHSL SMOD data.

    Args:
        config (dict): Configuration dictionary containing:
            - rasters_dir (str): Directory path to raster files.
            - ghsl_smod_file (str): Filename of the GHSL SMOD raster file.
        data (GeoDataFrame): Geospatial data containing geometries.

    Returns:
        GeoDataFrame: Updated geospatial data with a new 'rurban' classification column.
    """
    # Convert the coordinate reference system to ESRI:54009
    data = data.to_crs("ESRI:54009")

    # Extract coordinates from geometries
    coord_list = [(x, y) for x, y in zip(data["geometry"].x, data["geometry"].y)]

    # Path to the GHSL SMOD raster file
    raster_dir = os.path.join(os.getcwd(), config["rasters_dir"])
    ghsl_path = os.path.join(raster_dir, "ghsl", config["ghsl_smod_file"])

    # Sample raster values at the coordinates
    with rio.open(ghsl_path) as src:
        data["ghsl_smod"] = [x[0] for x in src.sample(coord_list)]

    # Define rural class codes
    rural = [10, 11, 12, 13]

    # Classify as 'urban' by default, then set to 'rural' where applicable
    data["rurban"] = "urban"
    data.loc[data["ghsl_smod"].isin(rural), "rurban"] = "rural"

    return data


def train_val_test_split(
    data: pd.DataFrame,
    test_size: float = 0.2,
    attributes: list = ["rurban"],
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Split data into training, validation, and test sets.

    Args:
        data (DataFrame): The dataset to be split.
        test_size (float, optional): Proportion of the dataset to include in the test split.
            Defaults to 0.2.
        attributes (list, optional): List of attributes to group by for stratified splitting.
            Defaults to ["rurban"].
        verbose (bool, optional): Whether to print detailed logging info. Defaults to True.

    Returns:
        DataFrame: The dataset with an additional 'dataset' column indicating
            train, val, or test split.
    """
    # Return if the dataset is already split
    if "dataset" in data.columns:
        return data

    # Initialize dataset column
    data["dataset"] = None
    total_size = len(data)
    test_size = int((total_size * test_size))
    logging.info(f"Data dimensions: {total_size}")

    # Make a copy of the data for manipulation
    test = data.copy()

    # Calculate value counts for stratification
    value_counts = data.groupby(attributes)[attributes[-1]].value_counts()
    value_counts = pd.DataFrame(value_counts).reset_index()

    for _, row in value_counts.iterrows():
        subtest = test.copy()
        # Filter data by stratification attributes
        for i in range(len(attributes)):
            subtest = subtest[subtest[attributes[i]] == row[attributes[i]]]

        # Determine size of the subtest split
        subtest_size = int(test_size * (row["count"] / total_size))
        if subtest_size > len(subtest):
            subtest_size = len(subtest)

        # Sample UIDs for the test split
        subtest_files = subtest.sample(subtest_size, random_state=SEED).UID.values
        in_test = data["UID"].isin(subtest_files)
        data.loc[in_test, "dataset"] = "test"

        # Sample UIDs for the validation split
        subval_files = (
            data[data.dataset != "test"]
            .sample(subtest_size, random_state=SEED)
            .UID.values
        )
        in_val = data["UID"].isin(subval_files)
        data.loc[in_val, "dataset"] = "val"

    # Fill remaining data as training split
    data.dataset = data.dataset.fillna("train")

    return data


def get_model_output(
    iso_code: str, config: dict, phase: str = "test", pretrained=None
) -> pd.DataFrame:
    """
    Retrieve the model output from a CSV file.

    Args:
        iso_code (str): ISO code of the dataset.
        config (dict): Configuration dictionary with keys:
            - config_name (str): Name of the configuration.
            - exp_dir (str): Directory where experiments are stored.
            - project (str): Name of the project.
        phase (str, optional): Phase of the data ('test', 'train', 'val'). Defaults to "test".

    Returns:
        pd.DataFrame: DataFrame containing the model output.
    """
    if pretrained:
        exp_name = f"{iso_code}_{config['config_name']}_{pretrained}"
    else:
        exp_name = f"{iso_code}_{config['config_name']}"

    filename = f"{exp_name}_{phase}.csv"
    output_path = os.path.join(
        os.getcwd(),
        config["exp_dir"],
        config["project"],
        exp_name,
        filename,
    )
    # logging.info(output_path)
    output = pd.read_csv(output_path)
    return output


def get_best_models(iso_code: str, config: dict = None) -> list:
    """
    Identifies the best-performing models based on their Area Under the Precision-Recall Curve (AUPRC).

    Args:
        iso_code (str): ISO code for the region being evaluated.
        config (dict): Configuration dictionary containing:
            - "all_models": A dictionary of model types and corresponding file paths.
            - "project": Name of the current project.

    Returns:
        list: A sorted list of the best model file paths, ranked by AUPRC.
    """
    # Initialize a dictionary to store AUPRC results for each model type
    config_results = dict()

    # Loop through each model type and its corresponding files
    for model_type in config["all_models"]:
        config_results[model_type] = dict()

        for model_file in config["all_models"][model_type]:
            # Load the configuration for the current model
            model_config = config_utils.load_config(model_file)
            # Update project name to match config
            if config:
                model_config["project"] = config["project"]

            # Get validation and test outputs
            val_output = get_model_output(
                iso_code, model_config, pretrained=None, phase="val"
            )
            test_output = get_model_output(
                iso_code, model_config, pretrained=None, phase="test"
            )

            # Evaluate model performance on validation data
            val_results = eval_utils.evaluate(
                y_true=val_output["y_true"],
                y_pred=val_output["y_preds"],
                y_prob=val_output["y_probs"],
                beta=2,
            )
            # Evaluate model performance on test data using the
            # optimal threshold from validation
            test_results = eval_utils.evaluate(
                y_true=test_output["y_true"],
                y_pred=test_output["y_preds"],
                y_prob=test_output["y_probs"],
                beta=2,
                optim_threshold=val_results["optim_threshold"],
            )

            # Store the test AUPRC for the current model
            config_results[model_type][model_file] = test_results["auprc"]

    # Identify the best model for each model type
    best_models = []
    for model_type in config_results:
        # Select the model with the highest AUPRC within the type
        best_model = max(config_results[model_type], key=config_results[model_type].get)
        best_models.append((best_model, config_results[model_type][best_model]))

    # Sort all best models across types by AUPRC in descending order
    best_models.sort(key=operator.itemgetter(1), reverse=True)

    # Extract just the model file paths
    best_models = [x[0] for x in best_models]

    return best_models


def get_ensemble_configs(iso_code: str, config: dict) -> list:
    """
    Loads and returns a list of model configuration files for ensemble predictions.

    Args:
        iso_code (str): ISO code for the country, used as a key in the configuration to find specific model files.
        config (dict): Configuration dictionary containing file paths for each country ISO code. The dictionary includes:
            - Each ISO code as a key, with a list of model file paths as its value.

    Returns:
        list: A list of model configuration dictionaries for each model file.
    """
    model_configs = []  # Initialize list to hold each loaded model configuration

    configs = get_best_models(iso_code, config)

    # Iterate over each model file associated with the given iso_code
    for model_file in configs:  # config[iso_code]:
        # Load the configuration file for the model
        model_config = config_utils.load_config(os.path.join(os.getcwd(), model_file))
        # Update project name to match config
        model_config["project"] = config["project"]
        # Add loaded configuration to the list
        model_configs.append(model_config)

    return model_configs


def ensemble_models(
    iso_code: str,
    config: dict,
    prob_col: str = "y_probs",
    phase: str = "val",
    pretrained: bool = None,
) -> pd.DataFrame:
    """
    Generates ensemble predictions by averaging the output probabilities of multiple models.

    Args:
        iso_code (str): ISO code for the country, used to identify the relevant models in the configuration.
        config (dict): Configuration dictionary containing model file paths for each country ISO code.
            The dictionary includes:
            - Each ISO code as a key, associated with a list of model configuration files.
        prob_col (str, optional): Column name containing model output probabilities to be ensembled.
            Default is "y_probs".
        phase (str, optional): Phase of the model (e.g., "train", "val", "test") to determine the data subset.
            Default is "val".
        pretrained (bool, optional): Flag to indicate if pretrained weights should be loaded. Default is None.

    Returns:
        pd.DataFrame: A DataFrame with ensemble-averaged probabilities in the specified probability column.
    """
    probs = 0  # Initialize cumulative probability sum

    # Load all model configurations for the specified iso_code
    model_configs = get_ensemble_configs(iso_code, config)

    # Loop through each model configuration to accumulate model output probabilities
    for model_config in model_configs:
        # Get model output for the specified phase
        output = get_model_output(
            iso_code, model_config, pretrained=pretrained, phase=phase
        )
        # Add the current model's probabilities to the cumulative sum
        probs = probs + output[prob_col].to_numpy()

    # Calculate the average probabilities across all models
    output[prob_col] = probs / len(model_configs)

    return output
