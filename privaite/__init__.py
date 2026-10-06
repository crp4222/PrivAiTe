import os

__version__ = "0.7.1"

# Dependencies that contact a third party on their own. The only request that
# may leave the machine is the one you make to your provider, so each of these
# is switched off before the library reads its environment. setdefault: a value
# the operator exported is kept.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")  # ONNX Runtime 1.29+, to Microsoft
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")  # Hugging Face Hub usage pings
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # price table from GitHub
