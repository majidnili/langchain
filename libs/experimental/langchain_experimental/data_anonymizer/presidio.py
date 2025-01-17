from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Union

import yaml

from langchain_experimental.data_anonymizer.base import (
    AnonymizerBase,
    ReversibleAnonymizerBase,
)
from langchain_experimental.data_anonymizer.deanonymizer_mapping import (
    DeanonymizerMapping,
    MappingDataType,
    create_anonymizer_mapping,
)
from langchain_experimental.data_anonymizer.deanonymizer_matching_strategies import (
    default_matching_strategy,
)
from langchain_experimental.data_anonymizer.faker_presidio_mapping import (
    get_pseudoanonymizer_mapping,
)

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

except ImportError as e:
    raise ImportError(
        "Could not import presidio_analyzer, please install with "
        "`pip install presidio-analyzer`. You will also need to download a "
        "spaCy model to use the analyzer, e.g. "
        "`python -m spacy download en_core_web_lg`."
    ) from e
try:
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig
except ImportError as e:
    raise ImportError(
        "Could not import presidio_anonymizer, please install with "
        "`pip install presidio-anonymizer`."
    ) from e

if TYPE_CHECKING:
    from presidio_analyzer import EntityRecognizer

# Configuring Anonymizer for multiple languages
# Detailed description and examples can be found here:
# langchain/docs/extras/guides/privacy/multi_language_anonymization.ipynb
DEFAULT_LANGUAGES_CONFIG = {
    # You can also use Stanza or transformers library.
    # See https://microsoft.github.io/presidio/analyzer/customizing_nlp_models/
    "nlp_engine_name": "spacy",
    "models": [
        {"lang_code": "en", "model_name": "en_core_web_lg"},
        # {"lang_code": "de", "model_name": "de_core_news_md"},
        # {"lang_code": "es", "model_name": "es_core_news_md"},
        # ...
        # List of available models: https://spacy.io/usage/models
    ],
}


class PresidioAnonymizerBase(AnonymizerBase):
    def __init__(
        self,
        analyzed_fields: Optional[List[str]] = None,
        operators: Optional[Dict[str, OperatorConfig]] = None,
        languages_config: Dict = DEFAULT_LANGUAGES_CONFIG,
        add_default_faker_operators: bool = True,
        faker_seed: Optional[int] = None,
    ):
        """
        Args:
            analyzed_fields: List of fields to detect and then anonymize.
                Defaults to all entities supported by Microsoft Presidio.
            operators: Operators to use for anonymization.
                Operators allow for custom anonymization of detected PII.
                Learn more:
                https://microsoft.github.io/presidio/tutorial/10_simple_anonymization/
            languages_config: Configuration for the NLP engine.
                First language in the list will be used as the main language
                in self.anonymize(...) when no language is specified.
                Learn more:
                https://microsoft.github.io/presidio/analyzer/customizing_nlp_models/
            faker_seed: Seed used to initialize faker.
                Defaults to None, in which case faker will be seeded randomly
                and provide random values.
        """
        self.analyzed_fields = (
            analyzed_fields
            if analyzed_fields is not None
            else list(get_pseudoanonymizer_mapping().keys())
        )

        if add_default_faker_operators:
            self.operators = {
                field: OperatorConfig(
                    operator_name="custom", params={"lambda": faker_function}
                )
                for field, faker_function in get_pseudoanonymizer_mapping(
                    faker_seed
                ).items()
            }
        else:
            self.operators = {}

        if operators:
            self.add_operators(operators)

        provider = NlpEngineProvider(nlp_configuration=languages_config)
        nlp_engine = provider.create_engine()

        self.supported_languages = list(nlp_engine.nlp.keys())

        self._analyzer = AnalyzerEngine(
            supported_languages=self.supported_languages, nlp_engine=nlp_engine
        )
        self._anonymizer = AnonymizerEngine()

    def add_recognizer(self, recognizer: EntityRecognizer) -> None:
        """Add a recognizer to the analyzer

        Args:
            recognizer: Recognizer to add to the analyzer.
        """
        self._analyzer.registry.add_recognizer(recognizer)
        self.analyzed_fields.extend(recognizer.supported_entities)

    def add_operators(self, operators: Dict[str, OperatorConfig]) -> None:
        """Add operators to the anonymizer

        Args:
            operators: Operators to add to the anonymizer.
        """
        self.operators.update(operators)


class PresidioAnonymizer(PresidioAnonymizerBase):
    def _anonymize(self, text: str, language: Optional[str] = None) -> str:
        """Anonymize text.
        Each PII entity is replaced with a fake value.
        Each time fake values will be different, as they are generated randomly.

        PresidioAnonymizer has no built-in memory -
        so it will not remember the effects of anonymizing previous texts.
        >>> anonymizer = PresidioAnonymizer()
        >>> anonymizer.anonymize("My name is John Doe. Hi John Doe!")
        'My name is Noah Rhodes. Hi Noah Rhodes!'
        >>> anonymizer.anonymize("My name is John Doe. Hi John Doe!")
        'My name is Brett Russell. Hi Brett Russell!'

        Args:
            text: text to anonymize
            language: language to use for analysis of PII
                If None, the first (main) language in the list
                of languages specified in the configuration will be used.
        """
        if language is None:
            language = self.supported_languages[0]

        if language not in self.supported_languages:
            raise ValueError(
                f"Language '{language}' is not supported. "
                f"Supported languages are: {self.supported_languages}. "
                "Change your language configuration file to add more languages."
            )

        analyzer_results = self._analyzer.analyze(
            text,
            entities=self.analyzed_fields,
            language=language,
        )

        filtered_analyzer_results = (
            self._anonymizer._remove_conflicts_and_get_text_manipulation_data(
                analyzer_results
            )
        )

        anonymizer_results = self._anonymizer.anonymize(
            text,
            analyzer_results=analyzer_results,
            operators=self.operators,
        )

        anonymizer_mapping = create_anonymizer_mapping(
            text,
            filtered_analyzer_results,
            anonymizer_results,
        )
        return default_matching_strategy(text, anonymizer_mapping)


class PresidioReversibleAnonymizer(PresidioAnonymizerBase, ReversibleAnonymizerBase):
    def __init__(
        self,
        analyzed_fields: Optional[List[str]] = None,
        operators: Optional[Dict[str, OperatorConfig]] = None,
        languages_config: Dict = DEFAULT_LANGUAGES_CONFIG,
        add_default_faker_operators: bool = True,
        faker_seed: Optional[int] = None,
    ):
        super().__init__(
            analyzed_fields,
            operators,
            languages_config,
            add_default_faker_operators,
            faker_seed,
        )
        self._deanonymizer_mapping = DeanonymizerMapping()

    @property
    def deanonymizer_mapping(self) -> MappingDataType:
        """Return the deanonymizer mapping"""
        return self._deanonymizer_mapping.data

    @property
    def anonymizer_mapping(self) -> MappingDataType:
        """Return the anonymizer mapping
        This is just the reverse version of the deanonymizer mapping."""
        return {
            key: {v: k for k, v in inner_dict.items()}
            for key, inner_dict in self.deanonymizer_mapping.items()
        }

    def _anonymize(self, text: str, language: Optional[str] = None) -> str:
        """Anonymize text.
        Each PII entity is replaced with a fake value.
        Each time fake values will be different, as they are generated randomly.
        At the same time, we will create a mapping from each anonymized entity
        back to its original text value.

        Thanks to the built-in memory, all previously anonymised entities
        will be remembered and replaced by the same fake values:
        >>> anonymizer = PresidioReversibleAnonymizer()
        >>> anonymizer.anonymize("My name is John Doe. Hi John Doe!")
        'My name is Noah Rhodes. Hi Noah Rhodes!'
        >>> anonymizer.anonymize("My name is John Doe. Hi John Doe!")
        'My name is Noah Rhodes. Hi Noah Rhodes!'

        Args:
            text: text to anonymize
            language: language to use for analysis of PII
                If None, the first (main) language in the list
                of languages specified in the configuration will be used.
        """
        if language is None:
            language = self.supported_languages[0]

        if language not in self.supported_languages:
            raise ValueError(
                f"Language '{language}' is not supported. "
                f"Supported languages are: {self.supported_languages}. "
                "Change your language configuration file to add more languages."
            )

        analyzer_results = self._analyzer.analyze(
            text,
            entities=self.analyzed_fields,
            language=language,
        )

        filtered_analyzer_results = (
            self._anonymizer._remove_conflicts_and_get_text_manipulation_data(
                analyzer_results
            )
        )

        anonymizer_results = self._anonymizer.anonymize(
            text,
            analyzer_results=analyzer_results,
            operators=self.operators,
        )

        new_deanonymizer_mapping = create_anonymizer_mapping(
            text,
            filtered_analyzer_results,
            anonymizer_results,
            is_reversed=True,
        )
        self._deanonymizer_mapping.update(new_deanonymizer_mapping)

        return default_matching_strategy(text, self.anonymizer_mapping)

    def _deanonymize(
        self,
        text_to_deanonymize: str,
        deanonymizer_matching_strategy: Callable[
            [str, MappingDataType], str
        ] = default_matching_strategy,
    ) -> str:
        """Deanonymize text.
        Each anonymized entity is replaced with its original value.
        This method exploits the mapping created during the anonymization process.

        Args:
            text_to_deanonymize: text to deanonymize
            deanonymizer_matching_strategy: function to use to match
                anonymized entities with their original values and replace them.
        """
        if not self._deanonymizer_mapping:
            raise ValueError(
                "Deanonymizer mapping is empty.",
                "Please call anonymize() and anonymize some text first.",
            )

        text_to_deanonymize = deanonymizer_matching_strategy(
            text_to_deanonymize, self.deanonymizer_mapping
        )

        return text_to_deanonymize

    def save_deanonymizer_mapping(self, file_path: Union[Path, str]) -> None:
        """Save the deanonymizer mapping to a JSON or YAML file.

        Args:
            file_path: Path to file to save the mapping to.

        Example:
        .. code-block:: python

            anonymizer.save_deanonymizer_mapping(file_path="path/mapping.json")
        """

        save_path = Path(file_path)

        if save_path.suffix not in [".json", ".yaml"]:
            raise ValueError(f"{save_path} must have an extension of .json or .yaml")

        # Make sure parent directories exist
        save_path.parent.mkdir(parents=True, exist_ok=True)

        if save_path.suffix == ".json":
            with open(save_path, "w") as f:
                json.dump(self.deanonymizer_mapping, f, indent=2)
        elif save_path.suffix == ".yaml":
            with open(save_path, "w") as f:
                yaml.dump(self.deanonymizer_mapping, f, default_flow_style=False)

    def load_deanonymizer_mapping(self, file_path: Union[Path, str]) -> None:
        """Load the deanonymizer mapping from a JSON or YAML file.

        Args:
            file_path: Path to file to load the mapping from.

        Example:
        .. code-block:: python

            anonymizer.load_deanonymizer_mapping(file_path="path/mapping.json")
        """

        load_path = Path(file_path)

        if load_path.suffix not in [".json", ".yaml"]:
            raise ValueError(f"{load_path} must have an extension of .json or .yaml")

        if load_path.suffix == ".json":
            with open(load_path, "r") as f:
                loaded_mapping = json.load(f)
        elif load_path.suffix == ".yaml":
            with open(load_path, "r") as f:
                loaded_mapping = yaml.load(f, Loader=yaml.FullLoader)

        self._deanonymizer_mapping.update(loaded_mapping)
