from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from typing import Any

from privaite.config.schema import PresidioDetectorConfig
from privaite.pii.boundaries import refine_boundary
from privaite.pii.detector_base import PIIDetector
from privaite.pii.entity import PIIEntity

logger = logging.getLogger("privaite.pii.detector_presidio")


def build_recognizers(
    lang: str,
    custom_patterns: Sequence[Any] = (),
    disabled: Sequence[str] = (),
) -> list[Any]:
    """The recognizers PrivAiTe adds on top of Presidio's own, for one language.

    Their supported entity types are exempt from the configured entity allowlist
    (see ``PresidioDetector.detect``), so anything reasoning about what Presidio
    can emit has to build the list here rather than restate it.

    A recognizer carrying language-specific vocabulary selects it from the
    language it is built for, so registering it everywhere costs nothing: the
    date recognizer used to apply French AND German months to every configured
    language (issue #31). Names in ``disabled`` are dropped, which is the only
    way to switch one off, since the allowlist deliberately does not scope
    them.
    """
    from privaite.pii.recognizer_context import ContextualNameRecognizer
    from privaite.pii.recognizer_fr_date import FrenchDateRecognizer
    from privaite.pii.recognizer_location import ContextualLocationRecognizer
    from privaite.pii.recognizer_secret import StructuredSecretRecognizer

    recognizers: list[Any] = [
        ContextualNameRecognizer(supported_language=lang),
        FrenchDateRecognizer(supported_language=lang),
        ContextualLocationRecognizer(supported_language=lang),
        StructuredSecretRecognizer(supported_language=lang),
    ]
    if custom_patterns:
        from privaite.pii.recognizer_custom import CustomPatternRecognizer

        recognizers.append(CustomPatternRecognizer(list(custom_patterns), supported_language=lang))
    if disabled:
        dropped = set(disabled)
        recognizers = [r for r in recognizers if r.name not in dropped]
    return recognizers


def builtin_recognizer_entity_types(disabled: Sequence[str] = ()) -> set[str]:
    """Entity types the recognizers PrivAiTe registers can emit, for these
    languages and with these recognizers disabled.

    They are exempt from the Presidio entity allowlist, so the engine's
    `block_entities` producible check must count them: without this, blocking
    LOCATION on a Presidio-only allowlist config would be refused at boot as
    unenforceable while the contextual location recognizer can in fact emit it.
    Disabling one narrows that set, so the check stays honest for an operator
    who switched a recognizer off.
    """
    return {t for rec in build_recognizers("en", disabled=disabled) for t in rec.supported_entities}


def blank_nlp_engine(languages: Sequence[str]) -> Any:
    """A Presidio spaCy engine on blank pipelines (`nlp_engine: blank`).

    Presidio reads tokens, lemmas and named entities from spaCy. A blank
    pipeline gives the tokens with no model to install and no network to run;
    the lemma of each token is its lower-cased form, so the context words of the
    pattern recognizers still match. There are no named entities: spaCy's
    PERSON, LOCATION and the other NER types are left to an ML detector.
    """
    import spacy
    from presidio_analyzer.nlp_engine import SpacyNlpEngine
    from spacy.language import Language

    if "privaite_lower_lemma" not in Language.factories:

        @Language.component("privaite_lower_lemma")
        def _lower_lemma(doc: Any) -> Any:
            for token in doc:
                token.lemma_ = token.lower_
            return doc

    class BlankSpacyNlpEngine(SpacyNlpEngine):  # type: ignore[misc]
        def load(self) -> None:
            pipelines: dict[str, Any] = {}
            for model in self.models:
                nlp = spacy.blank(model["lang_code"])
                nlp.add_pipe("privaite_lower_lemma")
                pipelines[model["lang_code"]] = nlp
            self.nlp = pipelines  # type: ignore[assignment]

    engine = BlankSpacyNlpEngine(
        models=[{"lang_code": lang, "model_name": f"blank:{lang}"} for lang in languages]
    )
    engine.load()
    return engine


def nlp_pieces(text: str, max_chars: int) -> list[tuple[int, int]]:
    """Consecutive ``[start, end)`` ranges that cover ``text``, none longer
    than ``max_chars``, for spaCy to parse one at a time.

    A cut moves back to the last line break, else the last space or tab, in
    the final quarter of the range, so spaCy rarely sees a word split; a run
    with no such break is cut where it is. Only spaCy sees these pieces: the
    recognizers run over the whole text.
    """
    pieces: list[tuple[int, int]] = []
    start, n = 0, len(text)
    while n - start > max_chars:
        end = start + max_chars
        floor = end - max_chars // 4
        cut = text.rfind("\n", floor, end)
        if cut < 0:
            cut = max(text.rfind(" ", floor, end), text.rfind("\t", floor, end))
        if cut >= 0:
            end = cut + 1
        pieces.append((start, end))
        start = end
    pieces.append((start, n))
    return pieces


def _use_bundled_suffix_list() -> None:
    """Keep tldextract from downloading the public suffix list.

    Presidio's email recognizer validates the domain with tldextract, whose
    default extractor fetches the list from publicsuffix.org on first use. The
    snapshot it ships with is enough to tell a real top-level domain, and
    needs no request.
    """
    try:
        from tldextract import tldextract
    except ImportError:  # pragma: no cover - presidio depends on it
        return
    tldextract.TLD_EXTRACTOR.suffix_list_urls = ()


class PresidioDetector(PIIDetector):
    def __init__(self, config: PresidioDetectorConfig, custom_patterns=None) -> None:
        self.config = config
        self._custom_patterns = custom_patterns or []
        self._analyzer: Any = None
        # The recognizers PrivAiTe registers itself, name -> entity types it can
        # emit. Filled by initialize() and used by detect() to exempt them from
        # the configured entity allowlist: a recognizer registered on every
        # analyzer must never be filtered out of every result.
        self._own_recognizers: dict[str, set[str]] = {}

    @property
    def name(self) -> str:
        return "presidio"

    async def initialize(self) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        _use_bundled_suffix_list()

        lang_model_map = {
            "en": "en_core_web_lg",
            "fr": "fr_core_news_md",
            "de": "de_core_news_md",
            "es": "es_core_news_md",
            "it": "it_core_news_md",
            "pt": "pt_core_news_md",
            "nl": "nl_core_news_md",
        }

        nlp_config: dict[str, Any] = {
            "nlp_engine_name": "spacy",
            "models": [],
        }

        unknown = [lang for lang in self.config.languages if lang not in lang_model_map]
        if unknown:
            # Silently dropping a language here used to pass init and then crash
            # EVERY request at detect() time (the analyze loop still used it).
            # Fail fast at startup with an actionable message instead.
            raise ValueError(
                f"No spaCy model mapping for language(s) {unknown}. "
                f"Supported: {sorted(lang_model_map)}"
            )

        for lang in self.config.languages:
            nlp_config["models"].append({"lang_code": lang, "model_name": lang_model_map[lang]})

        if not nlp_config["models"]:
            nlp_config["models"].append({"lang_code": "en", "model_name": "en_core_web_lg"})

        if self.config.nlp_engine == "blank":
            nlp_engine = blank_nlp_engine([m["lang_code"] for m in nlp_config["models"]])
        else:
            provider = NlpEngineProvider(nlp_configuration=nlp_config)
            nlp_engine = provider.create_engine()

        self._analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine,
            supported_languages=self.config.languages,
        )

        for lang in self.config.languages:
            for recognizer in build_recognizers(
                lang, self._custom_patterns, self.config.disabled_recognizers
            ):
                self._analyzer.registry.add_recognizer(recognizer)
                self._own_recognizers.setdefault(recognizer.name, set()).update(
                    recognizer.supported_entities
                )

        if self._custom_patterns:
            logger.info("Registered %d custom patterns", len(self._custom_patterns))

        # Discoverability: an operator who removes a type from `entities` expects
        # it gone, but the recognizers registered above are exempt from that
        # allowlist by design. Say so once at startup, naming the types and the
        # knob, rather than letting the config read as if it had scoped them.
        if self.config.entities:
            unscoped = {
                t
                for types in self._own_recognizers.values()
                for t in types
                if t not in set(self.config.entities)
            }
            if unscoped:
                logger.warning(
                    "Built-in recognizers can emit %s, which the entities "
                    "allowlist does not scope; disable them with "
                    "pii.detectors.presidio.disabled_recognizers if unwanted",
                    ", ".join(sorted(unscoped)),
                )

        logger.info(
            "Presidio analyzer initialized with languages: %s",
            self.config.languages,
        )

    async def detect(self, text: str, language: str = "en") -> list[PIIEntity]:
        if self._analyzer is None:
            raise RuntimeError("PresidioDetector not initialized")

        primary_lang = self.config.languages[0] if self.config.languages else "fr"
        # The configured allowlist (set by the onnx/max presets) scopes
        # Presidio's OWN recognizers to the types it is strong at. The
        # recognizers we register are exempt: a custom pattern is an explicit
        # operator opt-in, and a built-in contextual recognizer whose only type
        # the allowlist drops (ContextualLocationRecognizer emits LOCATION) was
        # dead code under the DEFAULT preset, registered on every analyzer and
        # filtered out of every result. Their types have to be requested from
        # the analyzer for them to run at all, which also lets Presidio's own
        # recognizers emit those types, so the results are filtered back by
        # recognizer below: what the allowlist scoped stays scoped.
        configured = set(self.config.entities) if self.config.entities else None
        own_types = {t for types in self._own_recognizers.values() for t in types}
        requested = None if configured is None else configured | own_types

        all_results = []
        seen_spans: set[tuple[int, int, str]] = set()

        max_chars = self.config.max_chunk_chars

        for lang in self.config.languages:
            # Long text is parsed by spaCy in pieces and analysed whole: see
            # _nlp_artifacts. None lets the analyzer parse it whole itself.
            nlp_artifacts = None
            if max_chars is not None and len(text) > max_chars:
                nlp_artifacts = await asyncio.to_thread(self._nlp_artifacts, text, lang, max_chars)
            results = await asyncio.to_thread(
                self._analyzer.analyze,
                text=text,
                language=lang,
                entities=list(requested) if requested else None,
                score_threshold=self.config.score_threshold,
                nlp_artifacts=nlp_artifacts,
            )
            for result in results:
                if requested and result.entity_type not in requested:
                    continue

                recognizer = result.recognition_metadata.get("recognizer_name", "")
                if (
                    configured is not None
                    and result.entity_type not in configured
                    and recognizer not in self._own_recognizers
                ):
                    continue
                if recognizer == "SpacyRecognizer" and lang != primary_lang:
                    continue

                span_key = (result.start, result.end, result.entity_type)
                if span_key not in seen_spans:
                    seen_spans.add(span_key)
                    all_results.append(result)

        pii_entities = []
        for result in all_results:
            span = text[result.start : result.end]

            recognizer = result.recognition_metadata.get("recognizer_name", "")
            if (
                result.entity_type == "PERSON"
                and recognizer == "SpacyRecognizer"
                and not _looks_like_name(span)
            ):
                continue
            pieces = [(result.start, result.end)]
            if result.entity_type == "DATE_TIME" and recognizer == "SpacyRecognizer":
                pieces = _date_pieces(text, result.start, result.end)

            for start, end in pieces:
                entity = PIIEntity(
                    entity_type=result.entity_type,
                    text=text[start:end],
                    start=start,
                    end=end,
                    score=result.score,
                    source="presidio",
                )
                # An explicit custom regex controls its own match/capture boundary.
                pii_entities.append(
                    entity
                    if recognizer == "CustomPatternRecognizer"
                    else refine_boundary(text, entity)
                )

        return pii_entities

    def _nlp_artifacts(self, text: str, language: str, max_chars: int) -> Any:
        """spaCy's analysis of ``text``, parsed in consecutive pieces of at most
        ``max_chars`` and joined back into one Doc.

        The parser's working memory is bounded by the piece, while every token,
        lemma and entity keeps its offset in the whole text, so the analyzer can
        run all its recognizers once over the whole text with these artifacts.
        """
        from spacy.tokens import Doc

        engine = self._analyzer.nlp_engine
        nlp = engine.nlp[language]
        docs = [nlp(text[start:end]) for start, end in nlp_pieces(text, max_chars)]
        doc = Doc.from_docs(docs, ensure_whitespace=False)
        # Offsets are only right if the joined Doc is the text, character for
        # character. A mismatch fails the detector, which blocks the request.
        if doc.text != text:
            raise RuntimeError("spaCy pieces did not rejoin to the original text")
        return engine._doc_to_nlp_artifact(doc, language)


# Code inside a spaCy date span: a call, a snake_case identifier, brackets, an
# assignment. Prose parentheses ("twenty (20) years") and line breaks are not
# code, so a span made only of those is kept exactly as spaCy reported it.
_DATE_CODE = re.compile(r"[\w)\]]\(|[^\W_]_[^\W_]|[{}\[\]=;`|$@]")
# Where a span that holds code is cut.
_DATE_BREAK = re.compile(r"[\n(){}\[\]<>=;_\\`\"|$#@]+")


def _date_pieces(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """The parts of a spaCy date span to keep.

    spaCy's English date entity also fires on code ("connect(api_key",
    "f.write(json.dumps(entry"). A span that holds code is cut at code
    characters and line breaks, and a piece is kept only if it has a digit, so
    the code goes back unchanged while every number the span covered stays
    covered. Any other span is kept as it is ("tomorrow", "October 9, 1954").
    """
    span = text[start:end]
    if not _DATE_CODE.search(span):
        return [(start, end)]
    pieces = []
    offset = 0
    for cut in [*_DATE_BREAK.finditer(span), None]:
        piece_end = cut.start() if cut else len(span)
        piece = span[offset:piece_end]
        if any(c.isdigit() for c in piece):
            left = len(piece) - len(piece.lstrip())
            right = len(piece.rstrip())
            pieces.append((start + offset + left, start + offset + right))
        offset = cut.end() if cut else len(span)
    return pieces


def _looks_like_name(text: str) -> bool:
    if "\n" in text:
        return False
    words = text.split()
    if len(words) > 4 or len(words) < 2:
        return False
    if any(c in text for c in "+={}[]<>;/\\@~#"):
        return False
    particles = {"de", "da", "di", "du", "von", "van", "del", "der", "le", "la"}
    if not all(w[0].isupper() or w in particles for w in words):
        return False
    return True
