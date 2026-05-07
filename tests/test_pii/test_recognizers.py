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
