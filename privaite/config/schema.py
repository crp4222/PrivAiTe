from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8400
    workers: int = 1
    log_level: str = "info"
    max_request_bytes: int = 10_000_000


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
    languages: list[str] = Field(default_factory=lambda: ["fr", "en"])
    score_threshold: float = 0.4
    entities: list[str] | None = None


# The openai/privacy-filter model's label set, shared by its ONNX and torch backends.
_PRIVACY_FILTER_LABELS = {
    "private_person": "PERSON",
    "private_email": "EMAIL_ADDRESS",
    "private_phone": "PHONE_NUMBER",
    "private_address": "LOCATION",
    "private_date": "DATE_TIME",
    "private_url": "URL",
    "account_number": "FINANCIAL",
    "secret": "SECRET",
}

# Immutable Hugging Face commits for the built-in detector models.  Keeping the
# defaults at commits rather than the moving `main` branch makes fresh installs
# reproducible.  Operators replacing model_name must set revision to a commit
# from that model's repository (or explicitly choose a mutable ref).
_PRIVACY_FILTER_REVISION = "7ffa9a043d54d1be65afb281eddf0ffbe629385b"
_BERT_NER_REVISION = "d1a3e8f13f8c3566299d95fcfc9a8d2382a9affc"
_GLINER_PII_REVISION = "1fcf13e85f4eef5394e1fcd406cf2ca9ea82351d"


class _PrivacyFilterDetectorConfig(BaseModel):
    """Shared config for the two openai/privacy-filter backends; they differ only in
    runtime knobs (ONNX variant vs torch dtype/cache)."""

    enabled: bool = False
    model_name: str = "openai/privacy-filter"
    # The built-in model is pinned to an immutable commit. When model_name is
    # changed, set its matching commit SHA too; None deliberately follows main.
    revision: str | None = _PRIVACY_FILTER_REVISION
    # Off by default: a PII proxy must not execute code shipped inside a model repo
    # unless the operator explicitly opts in for a custom-code model.
    trust_remote_code: bool = False
    device: str = "auto"
    score_threshold: float = 0.5
    label_mapping: dict[str, str] = Field(default_factory=lambda: dict(_PRIVACY_FILTER_LABELS))


class MLModelDetectorConfig(_PrivacyFilterDetectorConfig):
    torch_dtype: str = "float16"
    batch_size: int = 1


class OnnxDetectorConfig(_PrivacyFilterDetectorConfig):
    onnx_variant: str = "q4f16"
    max_length: int = 128000
    cache_dir: str | None = None


class BertNERDetectorConfig(BaseModel):
    enabled: bool = False
    model_name: str = "dslim/bert-base-NER"
    # The built-in model is pinned to an immutable commit; None follows main.
    revision: str | None = _BERT_NER_REVISION
    device: str = "auto"
    score_threshold: float = 0.5
    label_mapping: dict[str, str] = Field(
        default_factory=lambda: {
            "PER": "PERSON",
            "LOC": "LOCATION",
            "ORG": "ORGANIZATION",
        }
    )


class GlinerDetectorConfig(BaseModel):
    enabled: bool = False
    # GLiNER trained on independent (non-AI4Privacy) synthetic data. Adding it to
    # the onnx suite (the `max` preset) raises out-of-distribution recall; see the
    # OOD cross-check in privaite-bench. Needs torch + the gliner package
    # (pip install 'privaite[gliner]'); it is not part of the onnxruntime floor.
    model_name: str = "urchade/gliner_multi_pii-v1"
    # The built-in model is pinned to an immutable commit; None follows main.
    revision: str | None = _GLINER_PII_REVISION
    device: str = "auto"
    score_threshold: float = 0.5
    # GLiNER is label-conditioned: it only returns the labels asked for here.
    labels: list[str] = Field(
        default_factory=lambda: [
            "person",
            "first name",
            "last name",
            "email",
            "phone number",
            "address",
            "social security number",
            "iban",
            "swift bic code",
            "credit card number",
            "date of birth",
            "password",
            "ip address",
            "passport number",
        ]
    )
    # Map each requested GLiNER label to a canonical PrivAiTe entity type.
    label_mapping: dict[str, str] = Field(
        default_factory=lambda: {
            "person": "PERSON",
            "first name": "PERSON",
            "last name": "PERSON",
            "email": "EMAIL_ADDRESS",
            "phone number": "PHONE_NUMBER",
            "address": "LOCATION",
            "social security number": "US_SSN",
            "iban": "IBAN_CODE",
            "swift bic code": "FINANCIAL",
            "credit card number": "CREDIT_CARD",
            "date of birth": "DATE_TIME",
            "password": "SECRET",
            "ip address": "IP_ADDRESS",
            "passport number": "US_PASSPORT",
        }
    )


class DetectorsConfig(BaseModel):
    presidio: PresidioDetectorConfig = Field(default_factory=PresidioDetectorConfig)
    mlmodel: MLModelDetectorConfig = Field(default_factory=MLModelDetectorConfig)
    onnx: OnnxDetectorConfig = Field(default_factory=OnnxDetectorConfig)
    bert_ner: BertNERDetectorConfig = Field(default_factory=BertNERDetectorConfig)
    gliner: GlinerDetectorConfig = Field(default_factory=GlinerDetectorConfig)


class EntityOverride(BaseModel):
    method: str = "placeholder"
    masking_char: str = "*"


class AnonymizationConfig(BaseModel):
    method: str = "placeholder"
    faker_locale: list[str] = Field(default_factory=lambda: ["fr_FR", "en_US"])
    entity_overrides: dict[str, EntityOverride] = Field(default_factory=dict)


class DeanonymizationConfig(BaseModel):
    enabled: bool = True
    # Fuzzy matching catches placeholders the model re-typed imperfectly, at the
    # cost of a small wrong-substitution risk on lookalike spans, so it is opt-in.
    fuzzy_matching: bool = False
    fuzzy_threshold: float = 0.85


class InspectConfig(BaseModel):
    # Dry-run inspection endpoint (POST /v1/pii/inspect): returns the detections
    # for a caller-supplied text WITHOUT forwarding anything to any provider and
    # without logging any value. The caller already knows the text it sent, so
    # echoing its own detections back leaks nothing; it exists so operators can
    # verify what gets redacted before trusting the proxy. Off by default.
    enabled: bool = False


class CustomPatternConfig(BaseModel):
    pattern: str
    entity_type: str
    score: float = 0.9


class PassthroughConfig(BaseModel):
    system_messages: bool = False
    tool_calls: bool = False


# Presidio entity allowlist for the onnx/max presets: the checksummed/structured
# types Presidio is strong at, leaving names/addresses/secrets to the ML model.
_ONNX_PRESIDIO_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "DATE_TIME",
    "US_SSN",
    "UK_NHS",
]


class PIIConfig(BaseModel):
    enabled: bool = True
    # Default to the full ONNX suite (~84.5% recall on the benchmark, ~749ms):
    # it detects everything the light preset does plus secrets and passwords.
    # preset: "light" is the fast Presidio-only path (~62% recall, near-zero
    # latency); preset: null drives detectors by hand. Do NOT also pin
    # detectors.presidio.entities to a short allowlist on the light path: that
    # restricts it to those types only and drops recall to ~35%.
    preset: str | None = "onnx"
    detectors: DetectorsConfig = Field(default_factory=DetectorsConfig)
    custom_patterns: list[CustomPatternConfig] = Field(default_factory=list)
    merge_strategy: str = "union"
    overlap_resolution: str = "highest_score"
    # Fail closed by default: if anonymization raises, block the request rather
    # than forward raw PII. Only the explicit opt-out "allow" forwards on error.
    on_error: Literal["block", "allow"] = "block"
    # Hard policy gate: entity TYPES listed here cause the WHOLE request to be
    # rejected (HTTP 400) instead of pseudonymized — nothing is forwarded to the
    # provider. Empty by default, so the default behavior is unchanged (all
    # detected PII is masked with a placeholder). Opt-in per type, e.g.
    # ["US_SSN", "CREDIT_CARD"]; everything not listed is still masked as usual.
    block_entities: list[str] = Field(default_factory=list)
    strict: bool = False
    anonymization: AnonymizationConfig = Field(default_factory=AnonymizationConfig)
    deanonymization: DeanonymizationConfig = Field(default_factory=DeanonymizationConfig)
    passthrough: PassthroughConfig = Field(default_factory=PassthroughConfig)
    inspect: InspectConfig = Field(default_factory=InspectConfig)

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
                self.detectors.presidio.entities = list(_ONNX_PRESIDIO_ENTITIES)
            self.detectors.bert_ner.enabled = False
            self.detectors.mlmodel.enabled = False
            self.detectors.onnx.enabled = True
        elif self.preset == "max":
            # onnx suite + GLiNER (an independent, non-AI4Privacy model). Higher
            # out-of-distribution recall at the cost of more false positives and a
            # torch dependency; opt-in, never the default.
            self.detectors.presidio.enabled = True
            if not self.detectors.presidio.entities:
                self.detectors.presidio.entities = list(_ONNX_PRESIDIO_ENTITIES)
            self.detectors.bert_ner.enabled = False
            self.detectors.mlmodel.enabled = False
            self.detectors.onnx.enabled = True
            self.detectors.gliner.enabled = True
        else:
            raise ValueError(
                f"Unknown PII preset '{self.preset}'. "
                "Valid presets: light, standard, full, onnx, max"
            )


class LoggingConfig(BaseModel):
    format: str = "json"
    level: str = "info"


class GatewayUpstreamConfig(BaseModel):
    # The provider API root INCLUDING its version prefix; the gateway appends the
    # protocol's own suffix ("/messages", "/responses"). Codex subscription
    # traffic is served from https://chatgpt.com/backend-api/codex.
    base_url: str
    # Which client request headers to relay upstream. None (the default) is
    # transparent: every header is forwarded except the ones httpx derives from
    # the connection (host, content-length, and the encoding/keep-alive set). A
    # transparent relay is required because some providers select their request
    # schema from a header, so an allowlist that drops it breaks a valid body.
    # Set an explicit list only to deliberately restrict what leaves the machine.
    forward_headers: list[str] | None = None


class GatewayConfig(BaseModel):
    # Opt-in agent-CLI gateway: relays Anthropic Messages and OpenAI Responses
    # traffic through the same scrub/restore engine. The client authenticates
    # itself to the upstream with its own token; PrivAiTe neither injects nor
    # validates credentials on gateway routes. Off by default so nothing about
    # the proxy changes when disabled.
    enabled: bool = False
    anthropic: GatewayUpstreamConfig = Field(
        default_factory=lambda: GatewayUpstreamConfig(base_url="https://api.anthropic.com/v1")
    )
    openai_responses: GatewayUpstreamConfig = Field(
        default_factory=lambda: GatewayUpstreamConfig(base_url="https://api.openai.com/v1")
    )


class PrivAiTeConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    providers: list[ProviderConfig] = Field(default_factory=list)
    pii: PIIConfig = Field(default_factory=PIIConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
