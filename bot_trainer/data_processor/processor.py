import itertools
import os
from collections import ChainMap
from datetime import datetime
from pathlib import Path
from typing import Text, Dict, List

from loguru import logger as logging
from mongoengine import Document
from mongoengine.errors import DoesNotExist
from mongoengine.errors import NotUniqueError
from rasa.constants import DEFAULT_CONFIG_PATH, DEFAULT_DATA_PATH, DEFAULT_DOMAIN_PATH
from rasa.core.agent import Agent
from rasa.core.constants import INTENT_MESSAGE_PREFIX
from rasa.core.domain import InvalidDomain
from rasa.core.domain import SessionConfig
from rasa.core.events import Form, ActionExecuted, UserUttered
from rasa.core.slots import CategoricalSlot, FloatSlot
from rasa.core.training.structures import Checkpoint
from rasa.core.training.structures import STORY_START
from rasa.core.training.structures import StoryGraph, StoryStep, SlotSet
from rasa.data import get_core_nlu_files
from rasa.importers import utils
from rasa.importers.rasa import Domain, StoryFileReader
from rasa.nlu.training_data import Message, TrainingData
from rasa.train import DEFAULT_MODELS_PATH
from rasa.utils.endpoints import EndpointConfig
from rasa.utils.io import read_config_file

from bot_trainer.exceptions import AppException
from bot_trainer.utils import Utility
from .cache import InMemoryAgentCache
from .constant import (
    DOMAIN,
    SESSION_CONFIG,
    STORY_EVENT,
    REGEX_FEATURES,
    LOOKUP_TABLE,
    TRAINING_EXAMPLE,
    RESPONSE,
    ENTITY,
    SLOTS,
    MODEL_TRAINING_STATUS,
)
from .data_objects import (
    Responses,
    SessionConfigs,
    Configs,
    Endpoints,
    Entities,
    EntitySynonyms,
    TrainingExamples,
    Stories,
    Actions,
    Intents,
    Forms,
    LookupTables,
    RegexFeatures,
    Entity,
    EndPointBot,
    EndPointAction,
    EndPointTracker,
    Slots,
    StoryEvents,
    ModelTraining,
    ModelDeployment,
)


class MongoProcessor:
    """
    Class contains logic for saves, updates and deletes bot data in mongo database
    """

    async def upload_and_save(
        self,
        nlu: bytes,
        domain: bytes,
        stories: bytes,
        config: bytes,
        bot: Text,
        user: Text,
        overwrite: bool = True,
    ):
        """
        loads the training data into database

        :param nlu: nlu data
        :param domain: domain data
        :param stories: stories data
        :param config: config data
        :param bot: bot id
        :param user: user id
        :param overwrite: whether to append or overwrite, default is overwite
        :return: None
        """
        data_path = Utility.save_files(nlu, domain, stories, config)
        await self.save_from_path(data_path, bot, overwrite, user)
        Utility.delete_directory(data_path)

    def download_files(self, bot: Text):
        """
        create zip file containing download data

        :param bot: bot id
        :return: zip file path
        """
        nlu = self.load_nlu(bot)
        domain = self.load_domain(bot)
        stories = self.load_stories(bot)
        config = self.load_config(bot)
        return Utility.create_zip_file(nlu, domain, stories, config, bot)

    async def save_from_path(
        self, path: Text, bot: Text, overwrite: bool = True, user="default"
    ):
        """
        saves training data file

        :param path: data directory path
        :param bot: bot id
        :param overwrite: append or overwrite, default is overwrite
        :param user: user id
        :return: None
        """
        try:
            story_files, nlu_files = get_core_nlu_files(
                os.path.join(path, DEFAULT_DATA_PATH)
            )
            nlu = utils.training_data_from_paths(nlu_files, "en")
            domain = Domain.from_file(os.path.join(path, DEFAULT_DOMAIN_PATH))
            domain.check_missing_templates()
            story_steps = await StoryFileReader.read_from_files(story_files, domain)
            config = read_config_file(os.path.join(path, DEFAULT_CONFIG_PATH))

            if overwrite:
                self.delete_bot_data(bot, user)

            self.save_domain(domain, bot, user)
            self.save_stories(story_steps, bot, user)
            self.save_nlu(nlu, bot, user)
            self.save_config(config, bot, user)
        except InvalidDomain as e:
            logging.info(e)
            raise AppException(
                """Failed to validate yaml file.
                            Please make sure the file is initial and all mandatory parameters are specified"""
            )
        except Exception as e:
            logging.info(e)
            raise AppException(e)

    async def apply_template(self, template: Text, bot: Text, user: Text):
        """
        apply use-case template

        :param template: use-case template name
        :param bot: bot id
        :param user: user id
        :return: None
        :raises: raise AppException
        """
        use_case_path = os.path.join("./template/use-cases", template)
        if os.path.exists(use_case_path):
            await self.save_from_path(path=use_case_path, bot=bot, user=user)
        else:
            raise AppException("Invalid template!")

    def apply_config(self, template: Text, bot: Text, user: Text):
        """
        apply config template

        :param template: template name
        :param bot: bot id
        :param user: user id
        :return: None
        :raises: AppException
        """
        config_path = os.path.join("./template/config", template + ".yml")
        if os.path.exists(config_path):
            self.save_config(read_config_file(config_path), bot=bot, user=user)
        else:
            raise AppException("Invalid config!")

    def get_config_templates(self):
        """
        fetches list of available config template

        :return: config template list
        """
        files = Utility.list_files("./template/config")
        return [
            {"name": Path(file).stem, "config": read_config_file(file)}
            for file in files
        ]

    def delete_bot_data(self, bot: Text, user: Text):
        """
        deletes bot data

        :param bot: bot id
        :param user: user id
        :return: None
        """
        self.delete_domain(bot, user)
        self.delete_stories(bot, user)
        self.delete_nlu(bot, user)
        self.delete_config(bot, user)

    def save_nlu(self, nlu: TrainingData, bot: Text, user: Text):
        """
        saves training examples

        :param nlu: nly data
        :param bot: bot id
        :param user: user id
        :return: None
        """
        self.__save_training_examples(nlu.training_examples, bot, user)
        self.__save_entity_synonyms(nlu.entity_synonyms, bot, user)
        self.__save_lookup_tables(nlu.lookup_tables, bot, user)
        self.__save_regex_features(nlu.regex_features, bot, user)

    def delete_nlu(self, bot: Text, user: Text):
        """
        soft deletes nlu data

        :param bot: bot id
        :param user: user id
        :return: None
        """
        Utility.delete_document(
            [TrainingExamples, EntitySynonyms, LookupTables, RegexFeatures], user, bot
        )

    def load_nlu(self, bot: Text) -> TrainingData:
        """
        loads nlu data for training

        :param bot: bot id
        :return: TrainingData object
        """
        training_examples = self.__prepare_training_examples(bot)
        entity_synonyms = self.__prepare_training_synonyms(bot)
        lookup_tables = self.__prepare_training_lookup_tables(bot)
        regex_features = self.__prepare_training_regex_features(bot)
        return TrainingData(
            training_examples=training_examples,
            entity_synonyms=entity_synonyms,
            lookup_tables=lookup_tables,
            regex_features=regex_features,
        )

    def save_domain(self, domain: Domain, bot: Text, user: Text):
        """
        saves domain data

        :param domain: domain data
        :param bot: bot id
        :param user: user id
        :return: None
        """
        self.__save_intents(domain.intents, bot, user)
        self.__save_domain_entities(domain.entities, bot, user)
        self.__save_forms(domain.form_names, bot, user)
        self.__save_actions(domain.user_actions, bot, user)
        self.__save_responses(domain.templates, bot, user)
        self.__save_slots(domain.slots, bot, user)
        self.__save_session_config(domain.session_config, bot, user)

    def delete_domain(self, bot: Text, user: Text):
        """
        soft deletes domain data

        :param bot: bot id
        :param user: user id
        :return: None
        """
        Utility.delete_document(
            [Intents, Entities, Forms, Actions, Responses, Slots], bot, user
        )

    def load_domain(self, bot: Text) -> Domain:
        """
        loads domain data for training

        :param bot: bot id
        :return: dict of Domain objects
        """
        domain_dict = {
            DOMAIN.INTENTS.value: self.__prepare_training_intents(bot),
            DOMAIN.ACTIONS.value: self.__prepare_training_actions(bot),
            DOMAIN.SLOTS.value: self.__prepare_training_slots(bot),
            DOMAIN.SESSION_CONFIG.value: self.__prepare_training_session_config(bot),
            DOMAIN.RESPONSES.value: self.__prepare_training_responses(bot),
            DOMAIN.FORMS.value: self.__prepare_training_forms(bot),
            DOMAIN.ENTITIES.value: self.__prepare_training_domain_entities(bot),
        }
        return Domain.from_dict(domain_dict)

    def save_stories(self, story_steps: List[StoryStep], bot: Text, user: Text):
        """
        saves stories data

        :param story_steps: stories data
        :param bot: bot id
        :param user: user id
        :return: None
        """
        self.__save_stories(story_steps, bot, user)

    def delete_stories(self, bot: Text, user: Text):
        """
        soft deletes stories

        :param bot: bot id
        :param user: user id
        :return: None
        """
        Utility.delete_document([Stories], bot, user)

    def load_stories(self, bot: Text) -> StoryGraph:
        """
        loads stories for training

        :param bot: bot id
        :return: StoryGraph
        """
        return self.__prepare_training_story(bot)

    def __save_training_examples(self, training_examples, bot: Text, user: Text):
        if training_examples:
            new_examples = list(
                self.__extract_training_examples(training_examples, bot, user)
            )
            if new_examples:
                TrainingExamples.objects.insert(new_examples)

    def __extract_entities(self, entities):
        for entity in entities:
            entity_data = Entity(
                start=entity[ENTITY.START.value],
                end=entity[ENTITY.END.value],
                value=entity[ENTITY.VALUE.value],
                entity=entity[ENTITY.ENTITY.value],
            )
            yield entity_data

    def __extract_training_examples(self, training_examples, bot: Text, user: Text):
        saved_training_examples, _ = self.get_all_training_examples(bot)
        for training_example in training_examples:
            if str(training_example.text).lower() not in saved_training_examples:
                training_data = TrainingExamples()
                training_data.intent = training_example.data[
                    TRAINING_EXAMPLE.INTENT.value
                ]
                training_data.text = training_example.text
                training_data.bot = bot
                training_data.user = user
                if "entities" in training_example.data:
                    training_data.entities = list(
                        self.__extract_entities(
                            training_example.data[TRAINING_EXAMPLE.ENTITIES.value]
                        )
                    )
                yield training_data

    def __fetch_all_synonyms_value(self, bot: Text):
        synonyms = list(
            EntitySynonyms.objects(bot=bot, status=True).aggregate(
                [{"$group": {"_id": "$bot", "values": {"$push": "$value"},}}]
            )
        )
        if synonyms:
            return synonyms[0]["values"]
        else:
            return []

    def __extract_synonyms(self, synonyms, bot: Text, user: Text):
        saved_synonyms = self.__fetch_all_synonyms_value(bot)
        for key, value in synonyms.items():
            if key not in saved_synonyms:
                yield EntitySynonyms(bot=bot, synonym=value, value=key, user=user)

    def __save_entity_synonyms(self, entity_synonyms, bot: Text, user: Text):
        if entity_synonyms:
            new_synonyms = list(self.__extract_synonyms(entity_synonyms, bot, user))
            if new_synonyms:
                EntitySynonyms.objects.insert(new_synonyms)

    def fetch_synonyms(self, bot: Text, status=True):
        """
        fetches entity synonyms

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: yield name, value
        """
        entitySynonyms = EntitySynonyms.objects(bot=bot, status=status)
        for entitySynonym in entitySynonyms:
            yield {entitySynonym.value: entitySynonym.synonym}

    def __prepare_training_synonyms(self, bot: Text):
        synonyms = list(self.fetch_synonyms(bot))
        return dict(ChainMap(*synonyms))

    def __prepare_entities(self, entities):
        for entity in entities:
            yield entity.to_mongo().to_dict()

    def fetch_training_examples(self, bot: Text, status=True):
        """
        fetches training examples

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: Message List
        """
        trainingExamples = TrainingExamples.objects(bot=bot, status=status)
        for trainingExample in trainingExamples:
            message = Message(trainingExample.text)
            message.data = {TRAINING_EXAMPLE.INTENT.value: trainingExample.intent}
            if trainingExample.entities:
                message.data[TRAINING_EXAMPLE.ENTITIES.value] = list(
                    self.__prepare_entities(trainingExample.entities)
                )
            yield message

    def __prepare_training_examples(self, bot: Text):
        return list(self.fetch_training_examples(bot))

    def __fetch_all_lookup_values(self, bot: Text):
        lookup_tables = list(
            LookupTables.objects(bot=bot, status=True).aggregate(
                [{"$group": {"_id": "$bot", "values": {"$push": "$value"},}}]
            )
        )

        if lookup_tables:
            return lookup_tables[0]["values"]
        else:
            return []

    def __extract_lookup_tables(self, lookup_tables, bot: Text, user: Text):
        saved_lookup = self.__fetch_all_lookup_values(bot)
        for lookup_table in lookup_tables:
            name = lookup_table[LOOKUP_TABLE.NAME.value]
            for element in lookup_table[LOOKUP_TABLE.ELEMENTS.value]:
                if element not in saved_lookup:
                    yield LookupTables(name=name, value=element, bot=bot, user=user)

    def __save_lookup_tables(self, lookup_tables, bot: Text, user: Text):
        if lookup_tables:
            new_lookup = list(self.__extract_lookup_tables(lookup_tables, bot, user))
            if new_lookup:
                LookupTables.objects.insert(new_lookup)

    def fetch_lookup_tables(self, bot: Text, status=True):
        """
        fetches lookup table

        :param bot: bot id
        :param status: user id
        :return: yield dict of lookup tables
        """
        lookup_tables = LookupTables.objects(bot=bot, status=status).aggregate(
            [{"$group": {"_id": "$name", "elements": {"$push": "$value"}}}]
        )
        for lookup_table in lookup_tables:
            yield {
                LOOKUP_TABLE.NAME.value: lookup_table["_id"],
                LOOKUP_TABLE.ELEMENTS.value: lookup_table["elements"],
            }

    def __prepare_training_lookup_tables(self, bot: Text):
        return list(self.fetch_lookup_tables(bot))

    def __fetch_all_regex_patterns(self, bot: Text):
        regex_patterns = list(
            RegexFeatures.objects(bot=bot, status=True).aggregate(
                [{"$group": {"_id": "$bot", "patterns": {"$push": "$pattern"},}}]
            )
        )

        if regex_patterns:
            return regex_patterns[0]["patterns"]
        else:
            return []

    def __extract_regex_features(self, regex_features, bot: Text, user: Text):
        saved_regex_patterns = self.__fetch_all_regex_patterns(bot)
        for regex_feature in regex_features:
            if regex_feature["pattern"] not in saved_regex_patterns:
                regex_data = RegexFeatures(**regex_feature)
                regex_data.bot = bot
                regex_data.user = user
                yield regex_data

    def __save_regex_features(self, regex_features, bot: Text, user: Text):
        if regex_features:
            new_regex_patterns = list(
                self.__extract_regex_features(regex_features, bot, user)
            )
            if new_regex_patterns:
                RegexFeatures.objects.insert(new_regex_patterns)

    def fetch_regex_features(self, bot: Text, status=True):
        """
        fetches regular expression for entities

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: yield dict of regular expression
        """
        regex_features = RegexFeatures.objects(bot=bot, status=status)
        for regex_feature in regex_features:
            yield {
                REGEX_FEATURES.NAME.value: regex_feature["name"],
                REGEX_FEATURES.PATTERN.value: regex_feature["pattern"],
            }

    def __prepare_training_regex_features(self, bot: Text):
        return list(self.fetch_regex_features(bot))

    def __extract_intents(self, intents, bot: Text, user: Text):
        saved_intents = self.__prepare_training_intents(bot)
        for intent in intents:
            if intent not in saved_intents:
                yield Intents(name=intent, bot=bot, user=user)

    def __save_intents(self, intents, bot: Text, user: Text):
        if intents:
            new_intents = list(self.__extract_intents(intents, bot, user))
            if new_intents:
                Intents.objects.insert(new_intents)

    def fetch_intents(self, bot: Text, status=True):
        """
        fetches intents

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: List of intents
        """
        intents = Intents.objects(bot=bot, status=status).aggregate(
            [{"$group": {"_id": "$bot", "intents": {"$push": "$name"}}}]
        )
        return list(intents)

    def __prepare_training_intents(self, bot: Text):
        intents = self.fetch_intents(bot)
        if intents:
            return intents[0]["intents"]
        else:
            return []

    def __extract_domain_entities(self, entities: List[str], bot: Text, user: Text):
        saved_entities = self.__prepare_training_domain_entities(bot=bot)
        for entity in entities:
            if entity not in saved_entities:
                yield Entities(name=entity, bot=bot, user=user)

    def __save_domain_entities(self, entities: List[str], bot: Text, user: Text):
        if entities:
            new_entities = list(self.__extract_domain_entities(entities, bot, user))
            if new_entities:
                Entities.objects.insert(new_entities)

    def fetch_domain_entities(self, bot: Text, status=True):
        """
        fetches domain entities

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: list of entities
        """
        entities = Entities.objects(bot=bot, status=status).aggregate(
            [{"$group": {"_id": "$bot", "entities": {"$push": "$name"}}}]
        )
        return list(entities)

    def __prepare_training_domain_entities(self, bot: Text):
        entities = self.fetch_domain_entities(bot)
        if entities:
            return entities[0]["entities"]
        else:
            return []

    def __extract_forms(self, forms, bot: Text, user: Text):
        saved_forms = self.__prepare_training_forms(bot)
        for form in forms:
            if form not in saved_forms:
                yield Forms(name=form, bot=bot, user=user)

    def __save_forms(self, forms, bot: Text, user: Text):
        if forms:
            new_forms = list(self.__extract_forms(forms, bot, user))
            if new_forms:
                Forms.objects.insert(new_forms)

    def fetch_forms(self, bot: Text, status=True):
        """
        fetches form

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: list of forms
        """
        forms = Forms.objects(bot=bot, status=status).aggregate(
            [{"$group": {"_id": "$bot", "forms": {"$push": "$name"}}}]
        )
        return list(forms)

    def __prepare_training_forms(self, bot: Text):
        forms = self.fetch_forms(bot)
        if forms:
            return forms[0]["forms"]
        else:
            return []

    def __extract_actions(self, actions, bot: Text, user: Text):
        saved_actions = self.__prepare_training_actions(bot)
        for action in actions:
            if action not in saved_actions:
                yield Actions(name=action, bot=bot, user=user)

    def __save_actions(self, actions, bot: Text, user: Text):
        if actions:
            new_actions = list(self.__extract_actions(actions, bot, user))
            if new_actions:
                Actions.objects.insert(new_actions)

    def fetch_actions(self, bot: Text, status=True):
        """
        fetches actions

        :param bot: bot id
        :param status: user id
        :return: list of actions
        """
        actions = Actions.objects(bot=bot, status=status).aggregate(
            [{"$group": {"_id": "$bot", "actions": {"$push": "$name"}}}]
        )
        return list(actions)

    def __prepare_training_actions(self, bot: Text):
        actions = self.fetch_actions(bot)
        if actions:
            return actions[0]["actions"]
        else:
            return []

    def __extract_session_config(
        self, session_config: SessionConfig, bot: Text, user: Text
    ):
        return SessionConfigs(
            sesssionExpirationTime=session_config.session_expiration_time,
            carryOverSlots=session_config.carry_over_slots,
            bot=bot,
            user=user,
        )

    def __save_session_config(
        self, session_config: SessionConfig, bot: Text, user: Text
    ):
        try:
            if session_config:
                try:
                    session = SessionConfigs.objects().get(bot=bot)
                    session.session_expiration_time = (
                        session_config.session_expiration_time
                    )
                    session.carryOverSlots = True
                    session.user = user
                except DoesNotExist:
                    session = self.__extract_session_config(session_config, bot, user)
                session.save()

        except NotUniqueError as e:
            logging.info(e)
            raise AppException("Session Config already exists for the bot")
        except Exception as e:
            logging.info(e)
            raise AppException("Internal Server Error")

    def fetch_session_config(self, bot: Text):
        """
        fetches session config

        :param bot: bot id
        :return: SessionConfigs object
        """
        try:
            session_config = SessionConfigs.objects().get(bot=bot)
        except DoesNotExist as e:
            logging.info(e)
            session_config = None
        return session_config

    def __prepare_training_session_config(self, bot: Text):
        session_config = self.fetch_session_config(bot)
        if session_config:
            return {
                SESSION_CONFIG.SESSION_EXPIRATION_TIME.value: session_config.sesssionExpirationTime,
                SESSION_CONFIG.CARRY_OVER_SLOTS.value: session_config.carryOverSlots,
            }
        else:
            default_session = SessionConfig.default()
            return {
                SESSION_CONFIG.SESSION_EXPIRATION_TIME.value: default_session.session_expiration_time,
                SESSION_CONFIG.CARRY_OVER_SLOTS.value: default_session.carry_over_slots,
            }

    def __extract_response_value(self, values: List[Dict], key, bot: Text, user: Text):
        saved_responses = self.__fetch_list_of_response(bot)
        for value in values:
            if value not in saved_responses:
                response = Responses()
                response.name = key.strip()
                response.bot = bot
                response.user = user
                r_type, r_object = Utility.prepare_response(value)
                if RESPONSE.Text.value == r_type:
                    response.text = r_object
                elif RESPONSE.CUSTOM.value == r_type:
                    response.custom = r_object
                yield response

    def __extract_response(self, responses, bot: Text, user: Text):
        responses_result = []
        for key, values in responses.items():
            responses_to_saved = list(
                self.__extract_response_value(values, key, bot, user)
            )
            responses_result.extend(responses_to_saved)
        return responses_result

    def __save_responses(self, responses, bot: Text, user: Text):
        if responses:
            new_responses = self.__extract_response(responses, bot, user)
            if new_responses:
                Responses.objects.insert(new_responses)

    def __prepare_response_Text(self, texts: List[Dict]):
        for text in texts:
            yield text

    def fetch_responses(self, bot: Text, status=True):
        """
        fetches utterances

        :param bot: bot id
        :param status: active or inactive, default is True
        :return: yield bot utterances
        """
        responses = Responses.objects(bot=bot, status=status).aggregate(
            [
                {
                    "$group": {
                        "_id": "$name",
                        "texts": {"$push": "$text"},
                        "customs": {"$push": "$custom"},
                    }
                }
            ]
        )
        for response in responses:
            key = response["_id"]
            value = list(self.__prepare_response_Text(response["texts"]))
            if response["customs"]:
                value.extend(response["customs"])
            yield {key: value}

    def __prepare_training_responses(self, bot: Text):
        responses = dict(ChainMap(*list(self.fetch_responses(bot))))
        return responses

    def __fetch_slot_names(self, bot: Text):
        saved_slots = list(
            Slots.objects(bot=bot, status=True).aggregate(
                [{"$group": {"_id": "$bot", "slots": {"$push": "$name"}}}]
            )
        )
        slots_list = []
        if saved_slots:
            slots_list = saved_slots[0]["slots"]
        return slots_list

    def __extract_slots(self, slots, bot: Text, user: Text):
        slots_name_list = self.__fetch_slot_names(bot)
        for slot in slots:
            items = vars(slot)
            if items["name"] not in slots_name_list:
                items["type"] = slot.type_name
                items["value_reset_delay"] = items["_value_reset_delay"]
                items.pop("_value_reset_delay")
                items["bot"] = bot
                items["user"] = user
                items.pop("value")
                yield Slots._from_son(items)

    def __save_slots(self, slots, bot: Text, user: Text):
        if slots:
            new_slots = list(self.__extract_slots(slots, bot, user))
            if new_slots:
                Slots.objects.insert(new_slots)

    def fetch_slots(self, bot: Text, status=True):
        """
        fetches slots

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: list of slots
        """
        slots = Slots.objects(bot=bot, status=status)
        return list(slots)

    def __prepare_training_slots(self, bot: Text):
        slots = self.fetch_slots(bot)
        results = []
        for slot in slots:
            key = slot.name
            if slot.type == FloatSlot.type_name:
                value = {
                    SLOTS.INITIAL_VALUE.value: slot.initial_value,
                    SLOTS.VALUE_RESET_DELAY.value: slot.value_reset_delay,
                    SLOTS.AUTO_FILL.value: slot.auto_fill,
                    SLOTS.MIN_VALUE.value: slot.min_value,
                    SLOTS.MAX_VALUE.value: slot.max_value,
                }
            elif slot.type == CategoricalSlot.type_name:
                value = {
                    SLOTS.INITIAL_VALUE.value: slot.initial_value,
                    SLOTS.VALUE_RESET_DELAY.value: slot.value_reset_delay,
                    SLOTS.AUTO_FILL.value: slot.auto_fill,
                    SLOTS.VALUES.value: slot.values,
                }
            else:
                value = {
                    SLOTS.INITIAL_VALUE.value: slot.initial_value,
                    SLOTS.VALUE_RESET_DELAY.value: slot.value_reset_delay,
                    SLOTS.AUTO_FILL.value: slot.auto_fill,
                }
            value[SLOTS.TYPE.value] = slot.type
            results.append({key: value})
        return dict(ChainMap(*results))

    def __extract_story_events(self, events):
        for event in events:
            if isinstance(event, UserUttered):
                yield StoryEvents(type=event.type_name, name=event.text)
            elif isinstance(event, ActionExecuted):
                yield StoryEvents(type=event.type_name, name=event.action_name)
            elif isinstance(event, Form):
                yield StoryEvents(type=event.type_name, name=event.name)
            elif isinstance(event, SlotSet):
                yield StoryEvents(
                    type=event.type_name, name=event.key, value=event.value
                )

    def __fetch_story_block_names(self, bot: Text):
        saved_stories = list(
            Stories.objects(bot=bot, status=True).aggregate(
                [{"$group": {"_id": "$bot", "block": {"$push": "$block_name"},}}]
            )
        )
        result = []
        if saved_stories:
            result = saved_stories[0]["block"]
        return result

    def __extract_story_step(self, story_steps, bot: Text, user: Text):
        saved_stories = self.__fetch_story_block_names(bot)
        for story_step in story_steps:
            if story_step.block_name not in saved_stories:
                story_events = list(self.__extract_story_events(story_step.events))
                story = Stories(
                    block_name=story_step.block_name,
                    start_checkpoints=[
                        start_checkpoint.name
                        for start_checkpoint in story_step.start_checkpoints
                    ],
                    end_checkpoints=[
                        end_checkpoint.name
                        for end_checkpoint in story_step.end_checkpoints
                    ],
                    events=story_events,
                )
                story.bot = bot
                story.user = user
                yield story

    def __save_stories(self, story_steps, bot: Text, user: Text):
        if story_steps:
            new_stories = list(self.__extract_story_step(story_steps, bot, user))
            if new_stories:
                Stories.objects.insert(new_stories)

    def __prepare_training_story_events(self, events, timestamp):
        for event in events:
            if event.type == UserUttered.type_name:
                intent = {
                    STORY_EVENT.NAME.value: event.name,
                    STORY_EVENT.CONFIDENCE.value: 1.0,
                }
                parse_data = {
                    "text": INTENT_MESSAGE_PREFIX + event.name,
                    "intent": intent,
                    "intent_ranking": [intent],
                    "entities": [],
                }
                yield UserUttered(
                    text=event.name,
                    intent=intent,
                    parse_data=parse_data,
                    timestamp=timestamp,
                )
            elif event.type == ActionExecuted.type_name:
                yield ActionExecuted(action_name=event.name, timestamp=timestamp)
            elif event.type == Form.type_name:
                yield Form(name=event.name, timestamp=timestamp)
            elif event.type == SlotSet.type_name:
                yield SlotSet(key=event.name, value=event.value, timestamp=timestamp)

    def fetch_stories(self, bot: Text, status=True):
        """
        fetches stories

        :param bot: bot id
        :param status: active or inactive, default is active
        :return: list of stories
        """
        return list(Stories.objects(bot=bot, status=status))

    def __prepare_training_story_step(self, bot: Text):
        for story in Stories.objects(bot=bot, status=True):
            story_events = list(
                self.__prepare_training_story_events(
                    story.events, datetime.now().timestamp()
                )
            )
            yield StoryStep(
                block_name=story.block_name,
                events=story_events,
                start_checkpoints=[
                    Checkpoint(start_checkpoint)
                    for start_checkpoint in story.start_checkpoints
                ],
                end_checkpoints=[
                    Checkpoint(end_checkpoints)
                    for end_checkpoints in story.end_checkpoints
                ],
            )

    def __prepare_training_story(self, bot: Text):
        return StoryGraph(list(self.__prepare_training_story_step(bot)))

    def save_config(self, configs: dict, bot: Text, user: Text):
        """
        saves bot configuration

        :param configs: configuration
        :param bot: bot id
        :param user: user id
        :return: config unique id
        """
        try:
            Utility.validate_rasa_config(configs)
            config_obj = Configs.objects().get(bot=bot)
            config_obj.pipeline = configs["pipeline"]
            config_obj.language = configs["language"]
            config_obj.policies = configs["policies"]
        except DoesNotExist:
            configs["bot"] = bot
            configs["user"] = user
            config_obj = Configs._from_son(configs)
        except Exception as e:
            raise AppException(e)
        return config_obj.save().to_mongo().to_dict()["_id"].__str__()

    def delete_config(self, bot: Text, user: Text):
        """
        soft deletes bot training configuration

        :param bot: bot id
        :param user: user id
        :return: None
        """
        Utility.delete_document([Configs], bot, user)

    def fetch_configs(self, bot: Text):
        """
        fetches bot training configuration is exist otherwise load default

        :param bot: bot id
        :return: dict
        """
        try:
            configs = Configs.objects().get(bot=bot)
        except DoesNotExist as e:
            logging.info(e)
            configs = Configs._from_son(
                read_config_file("./template/config/default.yml")
            )
        return configs

    def load_config(self, bot: Text):
        """
        loads bot training configuration for training

        :param bot: bot id
        :return: dict
        """
        configs = self.fetch_configs(bot)
        config_dict = configs.to_mongo().to_dict()
        return {
            key: config_dict[key]
            for key in config_dict
            if key in ["language", "pipeline", "policies"]
        }

    def add_intent(self, text: Text, bot: Text, user: Text):
        """
        adds new intent

        :param text: intent name
        :param bot: bot id
        :param user: user id
        :return: intent id
        """
        if Utility.check_empty_string(text):
            raise AppException("Intent Name cannot be empty or blank spaces")

        Utility.is_exist(
            Intents,
            exp_message="Intent already exists!",
            name__iexact=text.strip(),
            bot=bot,
            status=True,
        )
        saved = Intents(name=text, bot=bot, user=user).save().to_mongo().to_dict()
        return saved["_id"].__str__()

    def get_intents(self, bot: Text):
        """
        fetches list of intent

        :param bot: bot id
        :return: intent list
        """
        intents = Intents.objects(bot=bot, status=True).order_by("-timestamp")
        return list(self.__prepare_document_list(intents, "name"))

    def add_training_example(
        self, examples: List[Text], intent: Text, bot: Text, user: Text
    ):
        """
        adds training examples for bot

        :param examples: list of training example
        :param intent: intent name
        :param bot: bot id
        :param user: user id
        :return: list training examples id
        """
        if Utility.check_empty_string(intent):
            raise AppException("Intent cannot be empty or blank spaces")
        if not Utility.is_exist(
            Intents, raise_error=False, name__iexact=intent, bot=bot, status=True
        ):
            self.add_intent(intent, bot, user)

        for example in examples:
            try:
                if Utility.check_empty_string(example):
                    raise AppException(
                        "Training Example cannot be empty or blank spaces"
                    )
                text, entities = Utility.extract_text_and_entities(example.strip())
                if Utility.is_exist(
                    TrainingExamples,
                    raise_error=False,
                    text__iexact=text,
                    bot=bot,
                    status=True,
                ):
                    yield {
                        "text": example,
                        "message": "Training Example already exists!",
                        "_id": None,
                    }
                else:
                    if entities:
                        ext_entity = [ent["entity"] for ent in entities]
                        self.__save_domain_entities(ext_entity, bot=bot, user=user)
                        self.__add_slots_from_entities(ext_entity, bot, user)
                        new_entities = list(self.__extract_entities(entities))
                    else:
                        new_entities = None

                    training_example = TrainingExamples(
                        intent=intent.strip(),
                        text=text,
                        entities=new_entities,
                        bot=bot,
                        user=user,
                    )

                    saved = training_example.save().to_mongo().to_dict()
                    yield {
                        "text": example,
                        "_id": saved["_id"].__str__(),
                        "message": "Training Example added successfully!",
                    }
            except Exception as e:
                yield {"text": example, "_id": None, "message": str(e)}

    def edit_training_example(
        self, id: Text, example: Text, intent: Text, bot: Text, user: Text
    ):
        """
        update training example

        :param id: training example id
        :param example: new training example
        :param intent: intent name
        :param bot: bot id
        :param user: user id
        :return: None
        """
        try:
            if Utility.check_empty_string(example):
                raise AppException("Training Example cannot be empty or blank spaces")
            text, entities = Utility.extract_text_and_entities(example.strip())
            if Utility.is_exist(
                TrainingExamples,
                raise_error=False,
                text__iexact=text,
                bot=bot,
                status=True,
            ):
                raise AppException("Training Example already exists!")
            training_example = TrainingExamples.objects(bot=bot, intent=intent).get(
                id=id
            )
            training_example.user = user
            training_example.text = text
            if entities:
                training_example.entities = self.__extract_entities(entities)
            training_example.timestamp = datetime.utcnow()
            training_example.save()
        except DoesNotExist as e:
            raise AppException("Invalid trianing example!")

    def search_training_examples(self, search: Text, bot: Text):
        """
        search the training examples

        :param search: search text
        :param bot: bot id
        :return: yields tuple of intent name, training example
        """
        results = (
            TrainingExamples.objects(bot=bot, status=True)
            .search_text(search)
            .order_by("$text_score")
            .limit(5)
        )
        for result in results:
            yield {"intent": result.intent, "text": result.text}

    def get_training_examples(self, intent: Text, bot: Text):
        """
        fetches training examples

        :param intent: intent name
        :param bot: bot id
        :return: yields training examples
        """
        training_examples = TrainingExamples.objects(
            bot=bot, intent__iexact=intent, status=True
        ).order_by("-timestamp")
        for training_example in training_examples:
            example = training_example.to_mongo().to_dict()
            entities = example["entities"] if "entities" in example else None
            yield {
                "_id": example["_id"].__str__(),
                "text": Utility.prepare_nlu_text(example["text"], entities),
            }

    def get_all_training_examples(self, bot: Text):
        """
        fetches list of all training examples

        :param bot: bot id
        :return: text list, id list
        """
        training_examples = list(
            TrainingExamples.objects(bot=bot, status=True).aggregate(
                [
                    {
                        "$group": {
                            "_id": "$bot",
                            "text": {"$push": {"$toLower": "$text"}},
                            "id": {"$push": {"$toString": "$_id"}},
                        }
                    }
                ]
            )
        )

        if training_examples:
            return training_examples[0]["text"], training_examples[0]["id"]
        else:
            return [], []

    def remove_document(
        self, document: Document, id: Text, bot: Text, user: Text, **kwargs
    ):
        """
        soft delete the document

        :param document: mongoengine document
        :param id: document id
        :param bot: bot id
        :param user: user id
        :return: None
        """
        try:
            doc = document.objects(bot=bot, **kwargs).get(id=id)
            doc.status = False
            doc.user = user
            doc.timestamp = datetime.utcnow()
            doc.save(validate=False)
        except DoesNotExist as e:
            logging.info(e)
            raise AppException("Unable to remove document")
        except Exception as e:
            logging.info(e)
            raise AppException("Unable to remove document")

    def __prepare_document_list(self, documents: List[Document], field: Text):
        for document in documents:
            doc_dict = document.to_mongo().to_dict()
            yield {"_id": doc_dict["_id"].__str__(), field: doc_dict[field]}

    def add_entity(self, name: Text, bot: Text, user: Text):
        """
        adds an entity

        :param name: entity name
        :param bot: bot id
        :param user: user id
        :return: entity id
        """
        if Utility.check_empty_string(name):
            raise AppException("Entity Name cannot be empty or blank spaces")
        Utility.is_exist(
            Entities,
            exp_message="Entity already exists!",
            name__iexact=name.strip(),
            bot=bot,
            status=True,
        )
        entity = (
            Entities(name=name.strip(), bot=bot, user=user).save().to_mongo().to_dict()
        )
        if not Utility.is_exist(
            Slots, raise_error=False, name__iexact=name, bot=bot, status=True
        ):
            Slots(name=name.strip(), type="text", bot=bot, user=user).save()
        return entity["_id"].__str__()

    def get_entities(self, bot: Text):
        """
        fetches list of registered entities

        :param bot: bot id
        :return: list of entities
        """
        entities = Entities.objects(bot=bot, status=True)
        return list(self.__prepare_document_list(entities, "name"))

    def add_action(self, name: Text, bot: Text, user: Text):
        """
        adds action
        :param name: action name
        :param bot: bot id
        :param user: user id
        :return: action id
        """
        if Utility.check_empty_string(name):
            raise AppException("Action name cannot be empty or blank spaces")
        Utility.is_exist(
            Actions,
            exp_message="Entity already exists!",
            name__iexact=name.strip(),
            bot=bot,
            status=True,
        )
        action = (
            Actions(name=name.strip(), bot=bot, user=user).save().to_mongo().to_dict()
        )
        return action["_id"].__str__()

    def get_actions(self, bot: Text):
        """
        fetches actions

        :param bot: bot id
        :return: list of actions
        """
        actions = Actions.objects(bot=bot, status=True)
        return list(self.__prepare_document_list(actions, "name"))

    def __add_slots_from_entities(self, entities: List[Text], bot: Text, user: Text):
        slot_name_list = self.__fetch_slot_names(bot)
        slots = [
            Slots(name=entity, type="text", bot=bot, user=user)
            for entity in entities
            if entity not in slot_name_list
        ]
        if slots:
            Slots.objects.insert(slots)

    def add_text_response(self, utterance: Text, name: Text, bot: Text, user: Text):
        """
        saves bot text utterance
        :param utterance: text utterance
        :param name: utterance name
        :param bot: bot id
        :param user: user id
        :return: bot utterance id
        """
        if Utility.check_empty_string(utterance):
            raise AppException("Utterance text cannot be empty or blank spaces")
        if Utility.check_empty_string(name):
            raise AppException("Utterance name cannot be empty or blank spaces")
        return self.add_response(
            utterances={"text": utterance.strip()}, name=name, bot=bot, user=user
        )

    def add_response(self, utterances: Dict, name: Text, bot: Text, user: Text):
        """
        save bot utterance

        :param utterances: utterance value
        :param name: utterance name
        :param bot: bot id
        :param user: user id
        :return: bot utterance id
        """
        self.__check_response_existence(
            response=utterances, bot=bot, exp_message="Utterance already exists!"
        )
        response = list(
            self.__extract_response_value(
                values=[utterances], key=name, bot=bot, user=user
            )
        )[0]
        value = response.save().to_mongo().to_dict()
        if not Utility.is_exist(
            Actions, raise_error=False, name__iexact=name, bot=bot, status=True
        ):
            Actions(name=name.strip(), bot=bot, user=user).save()
        return value["_id"].__str__()

    def edit_text_response(
        self, id: Text, utterance: Text, name: Text, bot: Text, user: Text
    ):
        """
        update the text bot utterance

        :param id: utterance id against which the utterance is updated
        :param utterance: text utterance value
        :param name: utterance name
        :param bot: bot id
        :param user: user id
        :return: None
        :raises: DoesNotExist: if utterance does not exist
        """
        self.edit_response(id, {"text": utterance}, name, bot, user)

    def edit_response(
        self, id: Text, utterances: Dict, name: Text, bot: Text, user: Text
    ):
        """
        update the bot utterance
        
        :param id: utterance id against which the utterance is updated
        :param utterances: utterance value
        :param name: utterance name
        :param bot: bot id
        :param user: user id
        :return: None
        :raises: AppException
        """
        try:
            self.__check_response_existence(
                response=utterances, bot=bot, exp_message="Utterance already exists!"
            )
            response = Responses.objects(bot=bot, name=name).get(id=id)
            r_type, r_object = Utility.prepare_response(utterances)
            if RESPONSE.Text.value == r_type:
                response.text = r_object
                response.custom = None
            elif RESPONSE.CUSTOM.value == r_type:
                response.custom = r_object
                response.text = None
            response.user = user
            response.timestamp = datetime.utcnow()
            response.save()
        except DoesNotExist as e:
            raise AppException("Utterance does not exist!")

    def get_response(self, name: Text, bot: Text):
        """
        fetch all the utterances
        
        :param name: utterance name
        :param bot: bot id
        :return: yields the utterances
        """
        values = Responses.objects(bot=bot, status=True, name__iexact=name).order_by(
            "-timestamp"
        )
        for value in values:
            val = None
            if value.text:
                val = list(
                    self.__prepare_response_Text([value.text.to_mongo().to_dict()])
                )[0]
            elif value.custom:
                val = value.custom.to_mongo().to_dict()
            yield {"_id": value.id.__str__(), "value": val}

    def __fetch_list_of_response(self, bot: Text):
        saved_responses = list(
            Responses.objects(bot=bot, status=True).aggregate(
                [
                    {
                        "$group": {
                            "_id": "$name",
                            "texts": {"$push": "$text"},
                            "customs": {"$push": "$custom"},
                        }
                    }
                ]
            )
        )

        saved_items = list(
            itertools.chain.from_iterable(
                [items["texts"] + items["customs"] for items in saved_responses]
            )
        )

        return saved_items

    def __check_response_existence(
        self, response: Dict, bot: Text, exp_message: Text = None, raise_error=True
    ):
        saved_items = self.__fetch_list_of_response(bot)

        if response in saved_items:
            if raise_error:
                if Utility.check_empty_string(exp_message):
                    raise AppException("Exception message cannot be empty")
                raise AppException(exp_message)
            else:
                return True
        else:
            if not raise_error:
                return False

    def add_story(self, name: Text, events: List[Dict], bot: Text, user: Text):
        """
        save story in mongodb
        
        :param name: story name
        :param events: story events list
        :param bot: bot id
        :param user: user id
        :return: story id
        :raises: AppException: Story already exist!

        :todo need to add the logic to add action, slots and forms if it does not exist
        """
        if Utility.check_empty_string(name):
            raise AppException("Story path name cannot be empty or blank spaces")

        self.__check_event_existence(
            events, bot=bot, exp_message="Story already exist!"
        )
        return (
            Stories(
                block_name=name.strip(),
                events=events,
                bot=bot,
                user=user,
                start_checkpoints=[STORY_START],
            )
            .save()
            .to_mongo()
            .to_dict()["_id"]
            .__str__()
        )

    def __fetch_list_of_events(self, bot: Text):
        saved_events = list(
            Stories.objects(bot=bot, status=True).aggregate(
                [{"$group": {"_id": "$name", "events": {"$push": "$events"}}}]
            )
        )

        saved_items = list(
            itertools.chain.from_iterable([items["events"] for items in saved_events])
        )

        return saved_items

    def __check_event_existence(
        self, events: List[Dict], bot: Text, exp_message: Text = None, raise_error=True
    ):
        saved_items = self.__fetch_list_of_events(bot)

        if events in saved_items:
            if raise_error:
                if Utility.check_empty_string(exp_message):
                    raise AppException("Exception message cannot be empty")
                raise AppException(exp_message)
            else:
                return True
        else:
            if not raise_error:
                return False

    def get_stories(self, bot: Text):
        """
        fetches stories 
        
        :param bot: bot is 
        :return: yield dict
        """
        for value in Stories.objects(bot=bot, status=True):
            item = value.to_mongo().to_dict()
            item.pop("bot")
            item.pop("user")
            item.pop("timestamp")
            item.pop("status")
            item["_id"] = item["_id"].__str__()
            yield item

    def get_utterance_from_intent(self, intent: Text, bot: Text):
        """
        fetches the utterance name by searching intent name in stories

        :param intent: intent name
        :param bot: bot id
        :return: utterance name
        """
        if Utility.check_empty_string(intent):
            raise AppException("Intent cannot be empty or blank spaces")

        responses = Responses.objects(bot=bot, status=True).distinct(field="name")
        story = Stories.objects(bot=bot, status=True, events__name__iexact=intent)
        if story:
            events = story[0].events
            search = False
            for i in range(len(events)):
                event = events[i]
                if event.type == "user":
                    if str(event.name).lower() == intent.lower():
                        search = True
                    else:
                        search = False
                if search and event.type == "action" and event.name in responses:
                    return event.name

    def add_session_config(
        self,
        bot: Text,
        user: Text,
        id: Text = None,
        sesssionExpirationTime: int = 60,
        carryOverSlots: bool = True,
    ):
        """
        save or update session config

        :param bot: bot id
        :param user: user id
        :param id: session config id
        :param sesssionExpirationTime: session expiration time, default is 60
        :param carryOverSlots: caary over slots, default is True
        :return:
        """
        if not Utility.check_empty_string(id):
            session_config = SessionConfigs.objects().get(id=id)
            session_config.sesssionExpirationTime = sesssionExpirationTime
            session_config.carryOverSlots = carryOverSlots
        else:
            if SessionConfigs.objects(bot=bot):
                raise AppException("Session config already exists!")
            session_config = SessionConfigs(
                sesssionExpirationTime=sesssionExpirationTime,
                carryOverSlots=carryOverSlots,
                bot=bot,
                user=user,
            )

        return session_config.save().to_mongo().to_dict()["_id"].__str__()

    def get_session_config(self, bot: Text):
        """
        fetches session configuration

        :param bot: bot id
        :return: dict of session configuration
        """
        session_config = SessionConfigs.objects().get(bot=bot).to_mongo().to_dict()
        return {
            "_id": session_config["_id"].__str__(),
            "sesssionExpirationTime": session_config["sesssionExpirationTime"],
            "carryOverSlots": session_config["carryOverSlots"],
        }

    def add_endpoints(self, endpoint_config: Dict, bot: Text, user: Text):
        """
        saves endpoint configurations

        :param endpoint_config: endpoint configurations
        :param bot: bot id
        :param user: user id
        :return: endpoint id
        """
        try:
            endpoint = Endpoints.objects().get(bot=bot)
        except DoesNotExist:
            if Endpoints.objects(bot=bot):
                raise AppException("Endpoint Configuration already exists!")
            endpoint = Endpoints()

        if endpoint_config.get("bot_endpoint"):
            endpoint.bot_endpoint = EndPointBot(**endpoint_config.get("bot_endpoint"))

        if endpoint_config.get("action_endpoint"):
            endpoint.action_endpoint = EndPointAction(
                **endpoint_config.get("action_endpoint")
            )

        if endpoint_config.get("tracker_endpoint"):
            endpoint.tracker_endpoint = EndPointTracker(
                **endpoint_config.get("tracker_endpoint")
            )

        endpoint.bot = bot
        endpoint.user = user
        return endpoint.save().to_mongo().to_dict()["_id"].__str__()

    def get_endpoints(self, bot: Text, raise_exception=True):
        """
        fetches endpoint configuration

        :param bot: bot id
        :param raise_exception: wether to raise an exception, default is True
        :return: endpoint configuration
        """
        try:
            endpoint = Endpoints.objects().get(bot=bot).to_mongo().to_dict()
            endpoint.pop("bot")
            endpoint.pop("user")
            endpoint.pop("timestamp")
            endpoint["_id"] = endpoint["_id"].__str__()
            return endpoint
        except DoesNotExist as e:
            logging.info(e)
            if raise_exception:
                raise AppException("Endpoint Configuration does not exists!")
            else:
                return {}

    def add_model_deployment_history(
        self, bot: Text, user: Text, model: Text, url: Text, status: Text
    ):
        """
        saves model deployment history

        :param bot: bot id
        :param user: user id
        :param model: model path
        :param url: deployment url
        :param status: deploument status
        :return: model deployment id
        """
        return (
            ModelDeployment(bot=bot, user=user, model=model, url=url, status=status)
            .save()
            .to_mongo()
            .to_dict()
            .get("_id")
            .__str__()
        )

    def get_model_deployment_history(self, bot: Text):
        """
        fetches model deployment history

        :param bot: bot id
        :return: list of model deployment history
        """
        model_deployments = ModelDeployment.objects(bot=bot).order_by("-timestamp")

        for deployment in model_deployments:
            value = deployment.to_mongo().to_dict()
            value.pop("bot")
            value.pop("_id")
            yield value

    def deploy_model(self, bot: Text, user: Text):
        """
        deploy the model to the particular url

        :param bot: bot id
        :param user: user id
        :return: deployment response
        :raises: Exception
        """
        endpoint = {}
        model = None
        try:
            endpoint = self.get_endpoints(bot, raise_exception=False)
            response, model = Utility.deploy_model(endpoint, bot)
        except Exception as e:
            response = str(e)

        self.add_model_deployment_history(
            bot=bot,
            user=user,
            model=model,
            url=(
                endpoint.get("bot_endpoint").get("url")
                if endpoint.get("bot_endpoint")
                else None
            ),
            status=response,
        )
        return response

    def delete_intent(
        self, intent: Text, bot: Text, user: Text, delete_dependencies=True
    ):
        """
        deletes intent including dependencies

        :param intent: intent name
        :param bot: bot id
        :param user: user id
        :param delete_dependencies: if True deletes training example, stories and responses
        that are associated with intent, default is True
        :return: None
        :raises: AppException
        """
        if Utility.check_empty_string(intent):
            raise AssertionError("Intent Name cannot be empty or blank spaces")
        try:
            # status to be filtered as Invalid Intent should not be fetched
            intentObj = Intents.objects(bot=bot, status=True).get(name__iexact=intent)
            intentObj.user = user
            intentObj.status = False
            intentObj.timestamp = datetime.utcnow()
            intentObj.save(validate=False)

            if delete_dependencies:
                Utility.delete_document(
                    [TrainingExamples], bot=bot, user=user, intent__iexact=intent
                )
                utterance_name = self.get_utterance_from_intent(intent, bot)
                if utterance_name:
                    Utility.delete_document(
                        [Responses], bot=bot, user=user, name__iexact=utterance_name
                    )
                Utility.delete_document(
                    [Stories], bot=bot, user=user, events__name__iexact=intent
                )
        except DoesNotExist as custEx:
            logging.info(custEx)
            raise AppException(
                "Invalid IntentName: Unable to remove document: " + str(custEx)
            )
        except Exception as ex:
            logging.info(ex)
            raise AppException("Unable to remove document" + str(ex))


class AgentProcessor:
    """
    Class contains logic for loading bot agents
    """

    mongo_processor = MongoProcessor()
    cache_provider = InMemoryAgentCache()

    @staticmethod
    def get_agent(bot: Text) -> Agent:
        """
        fetch the bot agent from cache if exist otherwise load it into the cache

        :param bot: bot id
        :return: Agent Object
        """
        if not AgentProcessor.cache_provider.is_exists(bot):
            AgentProcessor.reload(bot)
        return AgentProcessor.cache_provider.get(bot)

    @staticmethod
    def get_latest_model(bot: Text):
        """
        fetches the latest model from the path

        :param bot: bot id
        :return: latest model path
        """
        return Utility.get_latest_file(os.path.join(DEFAULT_MODELS_PATH, bot))

    @staticmethod
    def reload(bot: Text):
        """
        reload bot agent

        :param bot: bot id
        :return: None
        """
        try:
            endpoint = AgentProcessor.mongo_processor.get_endpoints(
                bot, raise_exception=False
            )
            action_endpoint = (
                EndpointConfig(url=endpoint["action_endpoint"]["url"])
                if endpoint and endpoint.get("action_endpoint")
                else None
            )
            model_path = AgentProcessor.get_latest_model(bot)
            domain = AgentProcessor.mongo_processor.load_domain(bot)
            mongo_store = Utility.get_local_mongo_store(bot, domain)
            agent = Agent.load(
                model_path, action_endpoint=action_endpoint, tracker_store=mongo_store
            )
            AgentProcessor.cache_provider.set(bot, agent)
        except Exception as e:
            logging.info(e)
            raise AppException("Bot has not been trained yet !")


class ModelProcessor:
    """
    Class contains logic for model training history
    """

    @staticmethod
    def set_training_status(
        bot: Text,
        user: Text,
        status: Text,
        model_path: Text = None,
        exception: Text = None,
    ):
        """
        add or update bot training history

        :param bot: bot id
        :param user: user id
        :param status: InProgress, Done, Fail
        :param model_path: new model path
        :param exception: exception while training
        :return: None
        """
        try:
            doc = ModelTraining.objects(bot=bot).get(
                status=MODEL_TRAINING_STATUS.INPROGRESS.value
            )
            doc.status = status
            doc.end_timestamp = datetime.utcnow()
        except DoesNotExist:
            doc = ModelTraining()
            doc.status = MODEL_TRAINING_STATUS.INPROGRESS.value
            doc.start_timestamp = datetime.utcnow()

        doc.bot = bot
        doc.user = user
        doc.model_path = model_path
        doc.exception = exception
        doc.save()

    @staticmethod
    def is_training_inprogress(bot: Text, raise_exception=True):
        """
        checks if there is any bot training in progress

        :param bot: bot id
        :param raise_exception: whether to raise an exception, default is True
        :return: None
        :raises: AppException
        """
        if ModelTraining.objects(
            bot=bot, status=MODEL_TRAINING_STATUS.INPROGRESS.value
        ).count():
            if raise_exception:
                raise AppException("Previous model training in progress.")
            else:
                return True
        else:
            return False

    @staticmethod
    def is_daily_training_limit_exceeded(bot: Text, raise_exception=True):
        """
        checks if daily bot training limit is exhausted

        :param bot: bot id
        :param raise_exception: whether to raise and exception
        :return: boolean
        :raises: AppException
        """
        today = datetime.today()

        today_start = today.replace(hour=0, minute=0, second=0)
        doc_count = ModelTraining.objects(
            bot=bot, start_timestamp__gte=today_start
        ).count()
        if doc_count >= Utility.environment["MODEL_TRAINING_LIMIT_PER_DAY"]:
            if raise_exception:
                raise AppException("Daily model training limit exceeded.")
            else:
                return True
        else:
            return False

    @staticmethod
    def get_training_history(bot: Text):
        """
        fetches bot training history

        :param bot: bot id
        :return: yield dict of training history
        """
        for value in ModelTraining.objects(bot=bot).order_by("-start_timestamp"):
            item = value.to_mongo().to_dict()
            item.pop("bot")
            item["_id"] = item["_id"].__str__()
            yield item
