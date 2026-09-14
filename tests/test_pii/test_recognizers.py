import pytest

from privaite.pii.recognizer_context import ContextualNameRecognizer
from privaite.pii.recognizer_fr_date import FrenchDateRecognizer


class TestContextualNameRecognizer:
    def setup_method(self):
        self.rec = ContextualNameRecognizer(supported_language="fr")

    def test_je_mappelle_lowercase(self):
        results = self.rec.analyze("je m'appelle dénis navarros", ["PERSON"], None)
        assert len(results) == 1
        text = "je m'appelle dénis navarros"
        assert text[results[0].start : results[0].end] == "dénis navarros"

    def test_my_name_is(self):
        results = self.rec.analyze("my name is john smith and hello", ["PERSON"], None)
        assert len(results) == 1
        text = "my name is john smith and hello"
        assert text[results[0].start : results[0].end] == "john smith"

    def test_je_suis(self):
        results = self.rec.analyze("je suis Marie Curie", ["PERSON"], None)
        assert len(results) == 1

    def test_mon_nom_est(self):
        results = self.rec.analyze("mon nom est Pierre Dupont", ["PERSON"], None)
        assert len(results) == 1

    def test_appelez_moi(self):
        results = self.rec.analyze("appelez-moi Jean", ["PERSON"], None)
        assert len(results) == 1

    def test_trims_at_stop_word(self):
        results = self.rec.analyze("je m'appelle Alice et je suis dev", ["PERSON"], None)
        text = "je m'appelle Alice et je suis dev"
        assert text[results[0].start : results[0].end] == "Alice"

    def test_no_match_on_plain_text(self):
        results = self.rec.analyze("bonjour comment allez-vous", ["PERSON"], None)
        assert len(results) == 0

    def test_accented_names(self):
        results = self.rec.analyze("je m'appelle Éloïse Bérénice", ["PERSON"], None)
        assert len(results) == 1

    def test_curly_apostrophe_intro(self):
        # macOS/iOS smart quotes emit U+2019; the intro must still match so the
        # lowercase name does not leak to the provider.
        text = "je m’appelle jean michel"
        results = self.rec.analyze(text, ["PERSON"], None)
        assert len(results) == 1
        assert text[results[0].start : results[0].end] == "jean michel"

    def test_double_space_in_name_keeps_exact_offsets(self):
        # a split/rejoin used to shorten the span by one char per extra space,
        # leaving the final character of the real name unmasked.
        text = "je m'appelle Jean  Dupont"
        results = self.rec.analyze(text, ["PERSON"], None)
        assert len(results) == 1
        assert text[results[0].start : results[0].end] == "Jean  Dupont"


class TestFrenchDateRecognizer:
    def setup_method(self):
        self.rec = FrenchDateRecognizer(supported_language="fr")

    def test_full_date(self):
        results = self.rec.analyze("née le 15 mars 1987 à Lyon", ["DATE_TIME"], None)
        assert len(results) >= 1
        text = "née le 15 mars 1987 à Lyon"
        matched = any(text[r.start : r.end] == "15 mars 1987" for r in results)
        assert matched

    def test_month_year(self):
        results = self.rec.analyze("en janvier 2024", ["DATE_TIME"], None)
        assert len(results) == 1

    def test_day_month(self):
        results = self.rec.analyze("le 3 décembre", ["DATE_TIME"], None)
        assert len(results) == 1

    def test_no_match(self):
        results = self.rec.analyze("il fait beau aujourd'hui", ["DATE_TIME"], None)
        assert len(results) == 0

    def test_accented_months(self):
        results = self.rec.analyze("le 1 février 2020", ["DATE_TIME"], None)
        assert len(results) >= 1

    def test_month_prefix_words_are_not_dates(self):
        # "mai(ntenant)" and "mars(eillais)" used to match through the missing
        # right boundary and split ordinary words.
        for text in ("il y en a 3 maintenant", "environ 1 marseillais", "2 maisons"):
            assert self.rec.analyze(text, ["DATE_TIME"], None) == []


class TestDateRecognizerLanguageScope:
    """Month vocabulary must belong to the language the recognizer serves.

    The French and German month lists used to be unioned and applied to every
    configured language, so an English or Dutch deployment masked exactly the
    months that are spelled like the German ones (April, August, September,
    November, juni, juli, oktober) and left the rest of the calendar alone.
    """

    EN = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    NL = [
        "januari",
        "februari",
        "maart",
        "april",
        "mei",
        "juni",
        "juli",
        "augustus",
        "september",
        "oktober",
        "november",
        "december",
    ]

    @pytest.mark.parametrize("lang", ["en", "nl", "fr"])
    def test_no_month_of_another_language_is_a_date(self, lang):
        rec = FrenchDateRecognizer(supported_language=lang)
        for month in self.EN + self.NL:
            assert rec.analyze(f"11 {month}", ["DATE_TIME"], None) == [], (
                f"{month!r} matched under language {lang!r}"
            )

    def test_french_months_match_only_under_french(self):
        assert FrenchDateRecognizer(supported_language="fr").analyze(
            "le 15 mars 1987", ["DATE_TIME"], None
        )
        assert (
            FrenchDateRecognizer(supported_language="de").analyze(
                "le 15 mars 1987", ["DATE_TIME"], None
            )
            == []
        )

    def test_german_months_match_only_under_german(self):
        assert FrenchDateRecognizer(supported_language="de").analyze(
            "am 15 März 1987", ["DATE_TIME"], None
        )
        assert (
            FrenchDateRecognizer(supported_language="fr").analyze(
                "am 15 März 1987", ["DATE_TIME"], None
            )
            == []
        )

    @pytest.mark.parametrize("lang", ["en", "nl", "fr", "de"])
    def test_the_numeric_birth_date_stays_active_in_every_language(self, lang):
        # It carries no month vocabulary, so scoping the whole recognizer by
        # language would have silently dropped it for English and Dutch.
        rec = FrenchDateRecognizer(supported_language=lang)
        assert rec.analyze("born 15/03/1987", ["DATE_TIME"], None)
