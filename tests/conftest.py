from __future__ import annotations

import pytest

from privaite.config.schema import (
    AnonymizationConfig,
    AuthConfig,
    DeanonymizationConfig,
    DetectorsConfig,
    LiteLLMParams,
    LoggingConfig,
    MLModelDetectorConfig,
    PassthroughConfig,
    PIIConfig,
    PresidioDetectorConfig,
    PrivAiTeConfig,
    ProviderConfig,
    ServerConfig,
)
from privaite.pii.mapping import PIIMapping


@pytest.fixture
def sample_config() -> PrivAiTeConfig:
    return PrivAiTeConfig(
        server=ServerConfig(host="127.0.0.1", port=8400),
        auth=AuthConfig(enabled=False),
        providers=[
            ProviderConfig(
                model_name="test-model",
                litellm_params=LiteLLMParams(model="openai/gpt-4o-mini"),
            )
        ],
        pii=PIIConfig(
            enabled=True,
            detectors=DetectorsConfig(
                presidio=PresidioDetectorConfig(enabled=False),
                mlmodel=MLModelDetectorConfig(enabled=False),
            ),
            anonymization=AnonymizationConfig(faker_locale=["fr_FR", "en_US"]),
            deanonymization=DeanonymizationConfig(enabled=True),
            passthrough=PassthroughConfig(),
        ),
        logging=LoggingConfig(format="text", level="debug"),
    )


@pytest.fixture
def pii_mapping_with_data() -> PIIMapping:
    mapping = PIIMapping()
    mapping.add("Jean Eude", "Michel Deus", "PERSON")
    mapping.add("jean@acme.com", "m.deus@example.net", "EMAIL_ADDRESS")
    mapping.add("+33 6 12 34 56 78", "+33 6 98 76 54 32", "PHONE_NUMBER")
    mapping.add("Paris", "Lyon", "LOCATION")
    return mapping
