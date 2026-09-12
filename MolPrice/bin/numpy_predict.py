import numpy as np
import pickle
import time
from rdkit import Chem
from rdkit.Chem import rdFingerprintGenerator
from rdkit import RDLogger
from pathlib import Path
from src.model_utils import MolFeatureExtractor
from tqdm import tqdm

RDLogger.DisableLog("rdApp.*")  # type: ignore
# Optional Ray import
try:
    import ray

    RAY_AVAILABLE = True
    # remove ray logging

except ImportError:
    RAY_AVAILABLE = False


def _smiles_to_fingerprint(smi, fp_gen, feature_gen):
    """Encode one valid molecule without substituting a synthetic fingerprint."""
    if not isinstance(smi, str) or not smi.strip():
        raise ValueError("SMILES must be a non-empty string")
    mol = Chem.MolFromSmiles(smi)
    if mol is None:
        raise ValueError(f"Invalid SMILES: {smi!r}")
    fp = fp_gen.GetFingerprintAsNumPy(mol).astype(np.float32)
    features = feature_gen.standardise_features(feature_gen.encode(smi))
    return np.concatenate((fp, features.reshape(-1))).astype(np.float32)


def _encode_smiles_batch(smiles_batch, fp_len, encode, errors):
    """Keep failed rows in place so predictions retain their input ordering."""
    fps = np.full((len(smiles_batch), fp_len + 10), np.nan, dtype=np.float32)
    for index, smi in enumerate(smiles_batch):
        try:
            if not isinstance(smi, str) or not smi.strip():
                raise ValueError("SMILES must be a non-empty string")
            fp = encode(smi)
            if fp.shape != (fp_len + 10,) or not np.isfinite(fp).all():
                raise ValueError("Could not generate a valid molecular fingerprint")
            fps[index] = fp
        except Exception:
            if errors == "raise":
                raise
    return fps


# Ray remote function for parallel fingerprint generation (must be at module level)
if RAY_AVAILABLE:

    @ray.remote
    def process_smiles_batch(smiles_batch, fp_rad, fp_len, data_path, errors="raise"):
        """Process a batch of SMILES in parallel"""
        from rdkit.Chem import rdFingerprintGenerator
        import sys
        from pathlib import Path
        
        # Add MolPrice root to path for Ray workers
        molprice_root = str(Path(__file__).resolve().parent.parent)
        if molprice_root not in sys.path:
            sys.path.insert(0, molprice_root)
            
        from src.model_utils import MolFeatureExtractor
        # Initialize generators for this worker
        fp_gen = rdFingerprintGenerator.GetMorganGenerator(
            radius=fp_rad, fpSize=fp_len, countSimulation=False
        )
        feature_gen = MolFeatureExtractor(Path(data_path))

        return _encode_smiles_batch(
            smiles_batch, fp_len,
            lambda smi: _smiles_to_fingerprint(smi, fp_gen, feature_gen),
            errors,
        )


class NumpyFingerprints:
    def __init__(self, weights_path=None, FP_rad=3, FP_len=4096, debug=False):
        """
        Standalone numpy implementation of Fingerprints model.

        Args:
            weights_path: Path to saved weights (.pickle or .pkl)
            FP_rad: Morgan fingerprint radius
            FP_len: Morgan fingerprint length
            debug: Enable debug output
        """
        self.FP_rad = FP_rad
        self.FP_len = FP_len
        self._restored = False
        self.debug = debug

        # Model weights storage
        self.nn_weights = []
        self.nn_biases = []
        self.final_weight = None
        self.final_bias = None

        # Initialize fingerprint generator
        self.fp_gen = rdFingerprintGenerator.GetMorganGenerator(
            radius=self.FP_rad, fpSize=self.FP_len, countSimulation=False
        )
        # Initialize feature extractor
        self.feature_gen = MolFeatureExtractor(
            Path(__file__).parent.parent / "data/features"
        )

        if weights_path:
            self.restore(weights_path)

    def restore(self, weights_path):
        """
        Load model weights from pickle file.

        Args:
            weights_path: Path to weights file (.pickle or .pkl)
        """
        with open(weights_path, "rb") as f:
            weights_dict = pickle.load(f)

        # Extract neural network layers and pre-transpose for efficiency
        layer_indices = [0, 3, 6, 9]  # Based on your model structure
        for idx in layer_indices:
            weight_key = f"neural_network.{idx}.weight"
            bias_key = f"neural_network.{idx}.bias"
            if weight_key in weights_dict and bias_key in weights_dict:
                # Pre-transpose weights to avoid doing it every forward pass
                self.nn_weights.append(weights_dict[weight_key].T)
                self.nn_biases.append(weights_dict[bias_key])

        # Final linear layer (also pre-transpose)
        if "linear.weight" in weights_dict and "linear.bias" in weights_dict:
            self.final_weight = weights_dict["linear.weight"].T
            self.final_bias = weights_dict["linear.bias"]

        self._restored = True
        print(f"Model restored from {weights_path}")
        return self

    def mol_to_fp(self, mol):
        """
        Convert RDKit molecule to Morgan fingerprint using new generator.

        Args:
            mol: RDKit molecule object

        Returns:
            numpy array fingerprint
        """
        if mol is None:
            return np.zeros(self.FP_len, dtype=np.float32)

        # Use the new fingerprint generator to get numpy array directly
        fp = self.fp_gen.GetFingerprintAsNumPy(mol)
        return fp.astype(np.float32)

    def smi_to_fp(self, smi):
        """
        Convert SMILES string to Morgan fingerprint.

        Args:
            smi: SMILES string

        Returns:
            numpy array fingerprint
        """
        if not smi:
            return np.zeros(self.FP_len, dtype=np.float32)

        return _smiles_to_fingerprint(smi, self.fp_gen, self.feature_gen)

    def relu(self, x):
        """ReLU activation function."""
        return np.maximum(0, x)

    def forward(self, x):
        """
        Forward pass through the network.

        Args:
            x: Input numpy array (fingerprint or batch of fingerprints)

        Returns:
            output: Final price
            z: Latent representation
        """
        if not self._restored:
            raise ValueError("Must restore model weights first!")

        start_time = time.time()
        z = x

        # Neural network layers
        for i, (weight, bias) in enumerate(zip(self.nn_weights, self.nn_biases)):
            z = z @ weight + bias  # Using @ operator instead of np.dot
            if i < len(self.nn_weights) - 1:  # Apply ReLU except for last layer
                z = self.relu(z)

        # Store intermediate representation
        intermediate = z.copy()

        # Final linear layer
        output = z @ self.final_weight + self.final_bias

        forward_time = time.time() - start_time
        if self.debug:
            print(f"Forward pass time: {forward_time:.4f} seconds")

        return output, intermediate

    def predict_from_smiles(self, smi, return_intermediate=False):
        """
        Get prediction directly from SMILES string.

        Args:
            smi: SMILES string
            return_intermediate: Whether to return intermediate representation

        Returns:
            price of molecule (and intermediate if requested)
        """
        fp = self.smi_to_fp(smi)
        if np.sum(fp) == 0:
            print("Warning: Could not generate fingerprint for SMILES")
            return 0.0 if not return_intermediate else (0.0, np.zeros(10))

        # Add batch dimension if single molecule
        if fp.ndim == 1:
            fp = fp.reshape(1, -1)

        output, intermediate = self.forward(fp)

        # Remove batch dimension if single molecule
        if output.shape[0] == 1:
            output = output.squeeze(0)
            intermediate = intermediate.squeeze(0)

        if return_intermediate:
            return output, intermediate
        return output[0]

    def predict_batch_from_smiles(
        self, smiles_list, return_intermediate=False, *, batch_size=256, errors="raise"
    ):
        """
        Get predictions for a batch of SMILES strings.

        Args:
            smiles_list: List of SMILES strings
            return_intermediate: Whether to return intermediate representations
            batch_size: Maximum molecules encoded and predicted at a time.
            errors: "raise" fails on invalid input or prediction errors. "coerce"
                keeps failed rows as NaN and retries failed batches one row at a time.

        Returns:
            Prices with shape (n, 1), and optionally intermediates with shape
            (n, 10), in the same order as smiles_list.
        """
        if (
            isinstance(batch_size, (bool, np.bool_))
            or not isinstance(batch_size, (int, np.integer))
            or batch_size < 1
        ):
            raise ValueError("batch_size must be a positive integer")
        if errors not in ("raise", "coerce"):
            raise ValueError("errors must be 'raise' or 'coerce'")
        num_smiles = len(smiles_list)
        if not num_smiles:
            raise ValueError("smiles_list must not be empty")
        if not self._restored:
            raise ValueError("Must restore model weights first!")

        use_ray = RAY_AVAILABLE and num_smiles > 100
        if use_ray:
            try:
                if not ray.is_initialized():
                    ray.init(ignore_reinit_error=True, log_to_driver=False)
            except Exception:
                if errors == "raise":
                    raise
                use_ray = False

        dtype = np.result_type(
            *self.nn_weights, *self.nn_biases, self.final_weight, self.final_bias, np.float32
        )
        output = np.full((num_smiles, 1), np.nan, dtype=dtype)
        intermediate = (
            np.full((num_smiles, 10), np.nan, dtype=dtype)
            if return_intermediate else None
        )

        def predict_rows(fps, indices):
            prices, latent = self.forward(fps)
            if prices.shape != (len(indices), 1) or not np.isfinite(prices).all():
                raise ValueError("Model returned invalid price predictions")
            if return_intermediate and (
                latent.shape != (len(indices), 10) or not np.isfinite(latent).all()
            ):
                raise ValueError("Model returned invalid intermediate representations")
            output[indices] = prices
            if return_intermediate:
                intermediate[indices] = latent

        for start in tqdm(
            range(0, num_smiles, batch_size),
            desc="Predicting molecules", disable=not self.debug,
        ):
            smiles_batch = smiles_list[start:start + batch_size]
            if use_ray:
                try:
                    fps = self._parallel_fingerprint_generation(smiles_batch, errors=errors)
                except Exception:
                    if errors == "raise":
                        raise
                    use_ray = False
                    fps = _encode_smiles_batch(smiles_batch, self.FP_len, self.smi_to_fp, errors)
            else:
                fps = _encode_smiles_batch(smiles_batch, self.FP_len, self.smi_to_fp, errors)

            valid = np.flatnonzero(np.isfinite(fps).all(axis=1))
            if not len(valid):
                continue
            try:
                predict_rows(fps[valid], start + valid)
            except Exception:
                if errors == "raise":
                    raise
                for index in valid:
                    try:
                        predict_rows(fps[index:index + 1], [start + index])
                    except Exception:
                        pass

        if return_intermediate:
            return output, intermediate
        return output

    def _parallel_fingerprint_generation(self, smiles_list, *, errors="raise"):
        """Generate only the current bounded slice of fingerprints using Ray."""
        # Split into batches for parallel processing
        num_cpus = max(1, int(ray.available_resources().get("CPU", 4)))
        batch_size = max(1, len(smiles_list) // (num_cpus * 2))

        batches = [
            smiles_list[i : i + batch_size]
            for i in range(0, len(smiles_list), batch_size)
        ]

        # Submit parallel tasks
        data_path = str(Path(__file__).parent.parent / "data/features")

        futures = []
        try:
            for batch in batches:
                futures.append(process_smiles_batch.remote(
                    batch, self.FP_rad, self.FP_len, data_path, errors
                ))
            return np.concatenate(ray.get(futures), axis=0)
        except Exception:
            for future in futures:
                try:
                    ray.cancel(future)
                except Exception:
                    pass
            raise


if __name__ == "__main__":
    import argparse
    import pandas as pd

    parser = argparse.ArgumentParser(description="Numpy Fingerprints Inference")
    parser.add_argument(
        "--mol",
        help="Path to the molecule file (.csv) or singular SMILES string",
        required=True,
    )

    parser.add_argument(
        "--smiles-col",
        help="Column name for SMILES string",
        type=str,
        default="smi_can",
    )
    args = parser.parse_args()

    weight_path = "models/Numpy/MP_Morgan_hybrid.pkl"
    model = NumpyFingerprints(weights_path=weight_path)
    if ".csv" in args.mol:
        print("-" * 50)
        print("DATA LOADING")
        print("-" * 50)
        print(f"Loading SMILES from {args.mol}")
        df = pd.read_csv(args.mol)
        smiles = df[args.smiles_col].tolist()
        print(f"Loaded {len(smiles)} SMILES from CSV")

        prediction = model.predict_batch_from_smiles(smiles)
        prediction = [pred[0] for pred in prediction]

        print("-" * 50)
        print("SAVING RESULTS")
        print("-" * 50)
        # Save predictions to file
        output_df = pd.DataFrame({"smi_can": smiles, "price": prediction})
        # replace 0 prices with "Error"
        output_df["price"] = output_df["price"].replace(0, "Error")
        output_df.to_csv("prices.csv", index=False)
        print(f"Predictions saved to prices.csv")
        print("-" * 50)
    else:
        print("-" * 50)
        print("SINGLE MOLECULE PREDICTION")
        print("-" * 50)
        prediction = model.predict_from_smiles(args.mol)
        print(f"Prediction for {args.mol}: {prediction:.2f}")
        print("-" * 50)
