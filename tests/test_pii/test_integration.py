import os
import time

import pytest

os.environ["TOKENIZERS_PARALLELISM"] = "false"


@pytest.fixture(scope="module")
def engine():
    import asyncio

    from privaite.config.schema import (
        AnonymizationConfig,
        DeanonymizationConfig,
        DetectorsConfig,
        PIIConfig,
        PresidioDetectorConfig,
    )
    from privaite.pii.engine import PIIEngine

    config = PIIConfig(
        enabled=True,
        preset=None,
        detectors=DetectorsConfig(
            presidio=PresidioDetectorConfig(
                enabled=True,
                languages=["fr", "en"],
                score_threshold=0.4,
                entities=[
                    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD",
                    "IBAN_CODE", "IP_ADDRESS", "LOCATION", "DATE_TIME",
                    "US_SSN", "URL",
                ],
            ),
        ),
        anonymization=AnonymizationConfig(faker_locale=["fr_FR", "en_US"]),
        deanonymization=DeanonymizationConfig(enabled=True),
    )
    eng = PIIEngine(config)
    asyncio.get_event_loop().run_until_complete(eng.initialize())
    yield eng
    asyncio.get_event_loop().run_until_complete(eng.shutdown())


HEAVY_TEXT_FR = (
    "Bonjour, je suis Marie-Claire Dupont-Moretti, née le 15 mars 1987 à Lyon. "
    "Mon adresse email est mc.dupont@gmail.com et mon numéro est le +33 6 78 45 12 90. "
    "J'habite au 42 rue Victor Hugo, 69002 Lyon. "
    "Mon IBAN est FR76 3000 6000 0112 3456 7890 189. "
    "J'ai rendez-vous avec Pierre-Antoine Lefebvre (pa.lefebvre@orange.fr, 06 12 98 76 54) "
    "le 23 décembre 2025 au restaurant Le Petit Zinc, 11 rue Saint-Benoît à Paris. "
    "Mon numéro de carte est 4111 1111 1111 1111. "
    "Mon IP est 192.168.1.42."
)

HEAVY_TEXT_EN = (
    "Hello, my name is John Alexander Smith and I live in New York. "
    "My email is john.smith@company.org and my phone is +1 212 555 0199. "
    "My SSN is 123-45-6789 and my credit card is 5500 0000 0000 0004. "
    "I work with Sarah Johnson (sarah.j@outlook.com, +44 7911 123456) "
    "at 350 Fifth Avenue, Manhattan. Our server IP is 10.0.0.42."
)

EXPECTED_PII_FR = {
    "Marie-Claire Dupont-Moretti": "PERSON",
    "mc.dupont@gmail.com": "EMAIL",
    "+33 6 78 45 12 90": "PHONE",
    "Lyon": "LOCATION",
    "FR76 3000 6000 0112 3456 7890 189": "IBAN",
    "Pierre-Antoine Lefebvre": "PERSON",
    "pa.lefebvre@orange.fr": "EMAIL",
    "06 12 98 76 54": "PHONE",
    "Paris": "LOCATION",
    "4111 1111 1111 1111": "CREDIT_CARD",
    "192.168.1.42": "IP",
    "15 mars 1987": "DATE",
}

EXPECTED_PII_EN = {
    "John Alexander Smith": "PERSON",
    "john.smith@company.org": "EMAIL",
    "+1 212 555 0199": "PHONE",
    "123-45-6789": "SSN",
    "5500 0000 0000 0004": "CREDIT_CARD",
    "Sarah Johnson": "PERSON",
    "sarah.j@outlook.com": "EMAIL",
    "+44 7911 123456": "PHONE",
    "10.0.0.42": "IP",
}

CONTEXTUAL_NAMES = [
    ("je m'appelle dénis navarros de la ciudad", "dénis navarros"),
    ("my name is alice wonderland and I code", "alice wonderland"),
    ("je me nomme François-Xavier de Villepin", "François-Xavier de Villepin"),
    ("appelez-moi Bob", "Bob"),
    ("mon nom est Éloïse Bérénice", "Éloïse Bérénice"),
]


@pytest.mark.asyncio
async def test_heavy_french_pii(engine):
    msgs = [{"role": "user", "content": HEAVY_TEXT_FR}]
    anon_msgs, mapping = await engine.process_request(msgs)
    anon = anon_msgs[0]["content"]

    detected = []
    leaked = []
    for pii, ptype in EXPECTED_PII_FR.items():
        if pii in anon:
            leaked.append((ptype, pii))
        else:
            detected.append((ptype, pii))

    assert len(leaked) <= 1, f"Too many PII leaked: {leaked}"
    assert len(detected) >= 10


@pytest.mark.asyncio
async def test_heavy_english_pii(engine):
    msgs = [{"role": "user", "content": HEAVY_TEXT_EN}]
    anon_msgs, mapping = await engine.process_request(msgs)
    anon = anon_msgs[0]["content"]

    detected = []
    leaked = []
    for pii, ptype in EXPECTED_PII_EN.items():
        if pii in anon:
            leaked.append((ptype, pii))
        else:
            detected.append((ptype, pii))

    assert len(leaked) <= 2, f"Too many PII leaked: {leaked}"
    assert len(detected) >= 7


@pytest.mark.asyncio
async def test_contextual_name_detection(engine):
    passed = 0
    for text, expected_name in CONTEXTUAL_NAMES:
        msgs = [{"role": "user", "content": text}]
        anon_msgs, mapping = await engine.process_request(msgs)
        if expected_name not in anon_msgs[0]["content"]:
            passed += 1

    assert passed >= 3, f"Only {passed}/{len(CONTEXTUAL_NAMES)} contextual names detected"


@pytest.mark.asyncio
async def test_deanonymization_roundtrip(engine):
    msgs = [{"role": "user", "content": HEAVY_TEXT_FR}]
    anon_msgs, mapping = await engine.process_request(msgs)

    fake_person = mapping.get_fake("Marie-Claire Dupont-Moretti")
    assert fake_person is not None

    fake_response = f"Bonjour {fake_person}, votre email est enregistré."
    result = await engine.process_response(fake_response, mapping)
    assert "Marie-Claire Dupont-Moretti" in result


@pytest.mark.asyncio
async def test_multi_turn_consistency(engine):
    text = "Je m'appelle Marie Dupont et mon email est marie@test.com"

    msgs1 = [{"role": "user", "content": text}]
    _, map1 = await engine.process_request(msgs1)
    fake1 = map1.get_fake("Marie Dupont")

    msgs2 = [{"role": "user", "content": text}]
    _, map2 = await engine.process_request(msgs2)
    fake2 = map2.get_fake("Marie Dupont")

    assert fake1 == fake2


@pytest.mark.asyncio
async def test_no_false_positives_on_clean_text(engine):
    clean_texts = [
        "Peux-tu me résumer ces informations dans un tableau ?",
        "Quel temps fait-il aujourd'hui ?",
        "Explique-moi comment fonctionne Python.",
        "Bonjour, comment allez-vous ? Je voudrais un café s'il vous plaît.",
    ]
    for text in clean_texts:
        msgs = [{"role": "user", "content": text}]
        anon_msgs, mapping = await engine.process_request(msgs)
        assert mapping.is_empty, (
            f"False positive on: {text} → "
            f"{list(mapping._original_to_fake.keys())}"
        )


@pytest.mark.asyncio
async def test_location_with_context_detected(engine):
    msgs = [{"role": "user", "content": "J'habite à Paris et je travaille à Lyon."}]
    _, mapping = await engine.process_request(msgs)
    assert not mapping.is_empty


@pytest.mark.asyncio
async def test_latency_single_request(engine):
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        msgs = [{"role": "user", "content": HEAVY_TEXT_FR}]
        await engine.process_request(msgs)
        times.append(time.perf_counter() - t0)

    avg_ms = (sum(times) / len(times)) * 1000
    p99_ms = sorted(times)[8] * 1000
    assert avg_ms < 2000, f"Too slow: avg={avg_ms:.0f}ms"
    assert p99_ms < 3000, f"P99 too slow: {p99_ms:.0f}ms"


@pytest.mark.asyncio
async def test_latency_short_text(engine):
    times = []
    for _ in range(20):
        t0 = time.perf_counter()
        msgs = [{"role": "user", "content": "je m'appelle Jean Dupont"}]
        await engine.process_request(msgs)
        times.append(time.perf_counter() - t0)

    avg_ms = (sum(times) / len(times)) * 1000
    assert avg_ms < 500, f"Too slow for short text: avg={avg_ms:.0f}ms"
