from __future__ import annotations

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8400
    workers: int = 1
    log_level: str = "info"


class AuthConfig(BaseModel):
    enabled: bool = True


class LiteLLMParams(BaseModel):
    model: str
    api_key: str | None = None
    api_base: str | None = None

    model_config = {"extra": "allow"}


class ProviderConfig(BaseModel):
    model_name: str
    litellm_params: LiteLLMParams


class PresidioDetectorConfig(BaseModel):
    enabled: bool = True
    languages: list[str] = Field(default_factory=lambda: ["en", "fr"])
    score_threshold: float = 0.4
    entities: list[str] | None = None


class MLModelDetectorConfig(BaseModel):
    enabled: bool = False
    model_name: str = "openai/privacy-filter"
    device: str = "auto"
    torch_dtype: str = "float16"
    score_threshold: float = 0.5
    batch_size: int = 1
    label_mapping: dict[str, str] = Field(default_factory=lambda: {
        "private_person": "PERSON",
        "private_email": "EMAIL_ADDRESS",
        "private_phone": "PHONE_NUMBER",
        "private_address": "LOCATION",
        "private_date": "DATE_TIME",
        "private_url": "URL",
        "account_number": "FINANCIAL",
        "secret": "SECRET",
    })


class OnnxDetectorConfig(BaseModel):
    enabled: bool = False
    model_name: str = "openai/privacy-filter"
    onnx_variant: str = "q4f16"
    device: str = "auto"
    score_threshold: float = 0.5
    max_length: int = 128000
    cache_dir: str | None = None
    label_mapping: dict[str, str] = Field(default_factory=lambda: {
        "private_person": "PERSON",
        "private_email": "EMAIL_ADDRESS",
        "private_phone": "PHONE_NUMBER",
        "private_date": "DATE_TIME",
        "account_number": "FINANCIAL",
        "secret": "SECRET",
    })


class BertNERDetectorConfig(BaseModel):
    enabled: bool = False
    model_name: str = "dslim/bert-base-NER"
    device: str = "auto"
    score_threshold: float = 0.5
    label_mapping: dict[str, str] = Field(default_factory=lambda: {
        "PER": "PERSON",
        "LOC": "LOCATION",
        "ORG": "ORGANIZATION",
    })


class DetectorsConfig(BaseModel):
    presidio: PresidioDetectorConfig = Field(default_factory=PresidioDetectorConfig)
    mlmodel: MLModelDetectorConfig = Field(default_factory=MLModelDetectorConfig)
    onnx: OnnxDetectorConfig = Field(default_factory=OnnxDetectorConfig)
    bert_ner: BertNERDetectorConfig = Field(default_factory=BertNERDetectorConfig)


class EntityOverride(BaseModel):
    method: str = "fake_replacement"
    masking_char: str = "*"
    domain_preserve: bool = False


class AnonymizationConfig(BaseModel):
    method: str = "fake_replacement"
    faker_locale: list[str] = Field(default_factory=lambda: ["fr_FR", "en_US"])
    entity_overrides: dict[str, EntityOverride] = Field(default_factory=dict)


class DeanonymizationConfig(BaseModel):
    enabled: bool = True
    fuzzy_matching: bool = True
    fuzzy_threshold: float = 0.85


class CustomPatternConfig(BaseModel):
    pattern: str
    entity_type: str
    score: float = 0.9


class PassthroughConfig(BaseModel):
    system_messages: bool = False
    tool_calls: bool = False


class PIIConfig(BaseModel):
    enabled: bool = True
    preset: str | None = None
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    custom_patterns: list[CustomPatternConfig] = Field(default_factory=list)
    merge_strategy: str = "union"
    overlap_resolution: str = "highest_score"
    on_error: str = "block"
    anonymization: AnonymizationConfig = Field(default_factory=AnonymizationConfig)
    deanonymization: DeanonymizationConfig = Field(default_factory=DeanonymizationConfig)
    passthrough: PassthroughConfig = Field(default_factory=PassthroughConfig)

    def model_post_init(self, __context: object) -> None:
        if self.preset is None:
            return
        if self.preset == "light":
            self.detectors.presidio.enabled = True
            self.detectors.bert_ner.enabled = False
            self.detectors.mlmodel.enabled = False
            self.detectors.onnx.enabled = False
        elif self.preset == "standard":
            self.detectors.presidio.enabled = True
            self.detectors.bert_ner.enabled = True
            self.detectors.mlmodel.enabled = False
            self.detectors.onnx.enabled = False
        elif self.preset == "full":
            self.detectors.presidio.enabled = True
            self.detectors.bert_ner.enabled = True
            self.detectors.mlmodel.enabled = True
            self.detectors.onnx.enabled = False
        elif self.preset == "onnx":
            self.detectors.presidio.enabled = True
            if not self.detectors.presidio.entities:
                self.detectors.presidio.entities = [
                    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
                    "IBAN_CODE", "IP_ADDRESS", "DATE_TIME", "US_SSN", "UK_NHS",
                ]
            self.detectors.bert_ner.enabled = False
            self.detectors.mlmodel.enabled = False
            self.detectors.onnx.enabled = True
        else:
            raise ValueError(
                f"Unknown PII preset '{self.preset}'. "
                "Valid presets: light, standard, full, onnx"
            )


class LoggingConfig(BaseModel):
    format: str = "json"
    level: str = "info"
    redact_fields: list[str] = Field(
        default_factory=lambda: ["messages", "content", "prompt"]
    )


class PrivAiTeConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)
    pii: PIIConfig = Field(default_factory=PIIConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
