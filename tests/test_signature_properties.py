import hashlib
import string
import unittest

from hypothesis import given, settings, strategies as st

from funding_bot import FundingBot


ASCII_TEXT = st.text(
    alphabet=string.ascii_letters + string.digits + " -_/:.&",
    max_size=40,
)
SCALAR_VALUES = st.one_of(
    ASCII_TEXT,
    st.integers(),
    st.booleans(),
    st.none(),
)
IDENTITY_FIELDS = st.fixed_dictionaries(
    {
        "source": SCALAR_VALUES,
        "portal_url": SCALAR_VALUES,
        "title": SCALAR_VALUES,
        "donor_name": SCALAR_VALUES,
    }
)
IRRELEVANT_FIELDS = st.dictionaries(
    st.sampled_from(["summary", "category", "tags", "status", "notes", "metadata"]),
    SCALAR_VALUES,
    max_size=6,
)


def _normalized_identity(opportunity: dict[str, object]) -> str:
    return "|".join(
        str(opportunity.get(field, "")).strip().lower()
        for field in ("source", "portal_url", "title", "donor_name")
    )


def _expected_signature(opportunity: dict[str, object]) -> str:
    return hashlib.sha256(_normalized_identity(opportunity).encode("utf-8")).hexdigest()


def _mutate_text(value: str, prefix: str, suffix: str, transform: str) -> str:
    transformed = {
        "identity": value,
        "upper": value.upper(),
        "lower": value.lower(),
        "swapcase": value.swapcase(),
        "title": value.title(),
    }[transform]
    return f"{prefix}{transformed}{suffix}"


class SignaturePropertyTests(unittest.TestCase):
    @settings(max_examples=2500, deadline=None)
    @given(identity_fields=IDENTITY_FIELDS, irrelevant_fields=IRRELEVANT_FIELDS)
    def test_signature_is_deterministic_and_matches_stable_hash_contract(
        self,
        identity_fields: dict[str, object],
        irrelevant_fields: dict[str, object],
    ) -> None:
        opportunity = {**identity_fields, **irrelevant_fields}
        reordered = dict(reversed(list(opportunity.items())))
        changed_irrelevant_fields = {**identity_fields, **irrelevant_fields, "summary": "changed"}

        signature = FundingBot._signature_for(opportunity)

        self.assertEqual(signature, FundingBot._signature_for(opportunity))
        self.assertEqual(signature, FundingBot._signature_for(reordered))
        self.assertEqual(signature, FundingBot._signature_for(changed_irrelevant_fields))
        self.assertEqual(signature, _expected_signature(opportunity))
        self.assertEqual(64, len(signature))

    @settings(max_examples=1500, deadline=None)
    @given(
        source=ASCII_TEXT,
        portal_url=ASCII_TEXT,
        title=ASCII_TEXT,
        donor_name=ASCII_TEXT,
        source_prefix=st.text(alphabet=" \t", max_size=3),
        source_suffix=st.text(alphabet=" \t", max_size=3),
        portal_prefix=st.text(alphabet=" \t", max_size=3),
        portal_suffix=st.text(alphabet=" \t", max_size=3),
        title_prefix=st.text(alphabet=" \t", max_size=3),
        title_suffix=st.text(alphabet=" \t", max_size=3),
        donor_prefix=st.text(alphabet=" \t", max_size=3),
        donor_suffix=st.text(alphabet=" \t", max_size=3),
        source_transform=st.sampled_from(["identity", "upper", "lower", "swapcase", "title"]),
        portal_transform=st.sampled_from(["identity", "upper", "lower", "swapcase", "title"]),
        title_transform=st.sampled_from(["identity", "upper", "lower", "swapcase", "title"]),
        donor_transform=st.sampled_from(["identity", "upper", "lower", "swapcase", "title"]),
    )
    def test_signature_is_stable_for_case_and_whitespace_equivalent_inputs(
        self,
        source: str,
        portal_url: str,
        title: str,
        donor_name: str,
        source_prefix: str,
        source_suffix: str,
        portal_prefix: str,
        portal_suffix: str,
        title_prefix: str,
        title_suffix: str,
        donor_prefix: str,
        donor_suffix: str,
        source_transform: str,
        portal_transform: str,
        title_transform: str,
        donor_transform: str,
    ) -> None:
        canonical = {
            "source": source,
            "portal_url": portal_url,
            "title": title,
            "donor_name": donor_name,
        }
        equivalent = {
            "source": _mutate_text(source, source_prefix, source_suffix, source_transform),
            "portal_url": _mutate_text(
                portal_url,
                portal_prefix,
                portal_suffix,
                portal_transform,
            ),
            "title": _mutate_text(title, title_prefix, title_suffix, title_transform),
            "donor_name": _mutate_text(
                donor_name,
                donor_prefix,
                donor_suffix,
                donor_transform,
            ),
        }

        self.assertEqual(
            FundingBot._signature_for(canonical),
            FundingBot._signature_for(equivalent),
        )
