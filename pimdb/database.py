import functools
import json
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple, Union, Callable, Any

from sqlalchemy import (
    Column,
    Boolean,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    ForeignKey,
    Index,
    text,
    and_,
)
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql import select
from sqlalchemy.sql.functions import coalesce
from sqlalchemy.sql.selectable import SelectBase

from pimdb.common import log, ImdbDataset, PimdbError, ReportTable, GzippedTsvReader, IMDB_DATASET_NAMES

#: Default number of bulk data (for e.g. SQL insert) to be collected in memory before they are sent to the database.
DEFAULT_BULK_SIZE = 1024

_TCONST_LENGTH = 12  # current maximum: 10
_NCONST_LENGTH = 12  # current maximum: 10
_TITLE_LENGTH = 512  # current maximum: 408
_NAME_LENGTH = 160  # current maximum: 105
_GENRE_LENGTH = 16
_GENRE_COUNT = 4
_REGION_LENGTH = 4
_LANGUAGE_LENGTH = 4
_CREW_COUNT = 2048  # current maximum: 1180
_PROFESSION_LENGTH = 32  # current maximum (from title.principals.category): 19
_PROFESSION_COUNT = 3
_JOB_LENGTH = 512  # current maximum: 286
_CHARACTER_LENGTH = 1024  # current maximum: 459
_CHARACTERS_LENGTH = 1024  # current maximum: 463
_KNOWN_FOR_TITLES_LENGTH = (_TCONST_LENGTH + 1) * 20 - 1  # current maximum: 159 resp. 15 titles

#: The "title_akas.types" field is a mess.
_ALIAS_TYPES_LENGTH = 64

#: The "title_akas.attributes" field is a mess.
_ATTRIBUTES_LENGTH = 128

IMDB_TITLE_ALIAS_TYPES = ["alternative", "dvd", "festival", "tv", "video", "working", "original", "imdbDisplay"]

_ALIAS_TYPE_LENGTH = max(len(item) for item in IMDB_TITLE_ALIAS_TYPES)
_ALIAS_TYPES_LENGTH = sum(len(item) for item in IMDB_TITLE_ALIAS_TYPES)

_TITLE_TYPE_LENGTH = 16  # TODO: document current maximum.


def imdb_dataset_table_infos() -> List[Tuple[ImdbDataset, List[Column]]]:
    """SQL tables that represent a direct copy of a TSV file (excluding duplicates)"""
    return [
        (
            ImdbDataset.TITLE_BASICS,
            [
                Column("tconst", String(_TCONST_LENGTH), nullable=False, primary_key=True),
                Column("titleType", String(_TITLE_TYPE_LENGTH), nullable=False),
                Column("primaryTitle", String(_TITLE_LENGTH)),
                Column("originalTitle", String(_TITLE_LENGTH)),
                Column("isAdult", Boolean, nullable=False),
                Column("startYear", Integer),
                Column("endYear", Integer),
                Column("runtimeMinutes", Integer),
                Column("genres", String((_GENRE_LENGTH + 1) * _GENRE_COUNT - 1)),
            ],
        ),
        (
            ImdbDataset.NAME_BASICS,
            [
                Column("nconst", String(_NCONST_LENGTH), nullable=False, primary_key=True),
                Column("primaryName", String(_NAME_LENGTH), nullable=False),
                Column("birthYear", Integer),
                Column("deathYear", Integer),
                Column("primaryProfession", String((_PROFESSION_LENGTH + 1) * _PROFESSION_COUNT - 1)),
                Column("knownForTitles", String(_KNOWN_FOR_TITLES_LENGTH)),
            ],
        ),
        (
            ImdbDataset.TITLE_AKAS,
            [
                Column("titleId", String(_TCONST_LENGTH), nullable=False, primary_key=True),
                Column("ordering", Integer, nullable=False, primary_key=True),
                Column("title", String(_TITLE_LENGTH)),
                Column("region", String(_REGION_LENGTH)),
                Column("language", String(_LANGUAGE_LENGTH)),
                Column("types", String(_ALIAS_TYPES_LENGTH)),
                Column("attributes", String(_ATTRIBUTES_LENGTH)),
                # NOTE: isOriginalTitle sometimes actually is null.
                Column("isOriginalTitle", Boolean),
            ],
        ),
        (
            ImdbDataset.TITLE_CREW,
            [
                Column("tconst", String(_TCONST_LENGTH), nullable=False, primary_key=True),
                Column("directors", String((_NCONST_LENGTH + 1) * _CREW_COUNT - 1)),
                Column("writers", String((_NCONST_LENGTH + 1) * _CREW_COUNT - 1)),
            ],
        ),
        (
            ImdbDataset.TITLE_PRINCIPALS,
            [
                Column("tconst", String(_TCONST_LENGTH), nullable=False, primary_key=True),
                Column("ordering", Integer, nullable=False, primary_key=True),
                Column("nconst", String(_NCONST_LENGTH)),
                Column("category", String(_PROFESSION_LENGTH)),
                Column("job", String(_JOB_LENGTH)),
                Column("characters", String(_CHARACTERS_LENGTH)),
            ],
        ),
        (
            ImdbDataset.TITLE_RATINGS,
            [
                Column("tconst", String, nullable=False, primary_key=True),
                Column("averageRating", Float, nullable=False),
                Column("numVotes", Integer, nullable=False),
            ],
        ),
    ]


def _key_table_info(report_table: ReportTable, name_length: int) -> Tuple[ReportTable, List[Union[Column, Index]]]:
    assert isinstance(report_table, ReportTable)
    return (
        report_table,
        [
            Column("id", Integer, nullable=False, primary_key=True),
            Column("name", String(name_length), nullable=False),
            Index(f"index__{report_table.value}__name", "name", unique=True),
        ],
    )


def _ordered_relation_table_info(
    table_to_create: ReportTable, from_table: ReportTable, to_table: ReportTable
) -> Tuple[ReportTable, List[Union[Column, Index]]]:
    """
    Information required to create a table representing an ordered relation
    pointing from ``from_table`` to ``to_table``, including the necessary
    indexes and constraints.
    """
    assert isinstance(table_to_create, ReportTable)
    assert isinstance(from_table, ReportTable)
    assert isinstance(to_table, ReportTable)
    report_table_name = table_to_create.value
    from_table_name = from_table.value
    to_table_name = to_table.value
    return (
        table_to_create,
        [
            Column(f"{from_table_name}_id", Integer, ForeignKey(f"{from_table_name}.id"), nullable=False),
            Column("ordering", Integer, nullable=False),
            Column(f"{to_table_name}_id", Integer, ForeignKey(f"{to_table_name}.id"), nullable=False),
            Index(
                f"index__{report_table_name}__{from_table_name}_id__ordering",
                f"{from_table_name}_id",
                "ordering",
                unique=True,
            ),
            Index(f"index__{report_table_name}__{to_table_name}_id", f"{to_table_name}_id"),
        ],
    )


def report_table_infos() -> List[Tuple[ReportTable, List[Union[Column, Index]]]]:
    return [
        _key_table_info(ReportTable.CHARACTER, _CHARACTER_LENGTH),
        (
            ReportTable.CHARACTERS_TO_CHARACTER,
            [
                Column(f"characters", String(_CHARACTERS_LENGTH), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column(f"character_id", Integer, ForeignKey(f"character.id"), nullable=False),
                Index("index__name__characters__ordering", "characters", "ordering", unique=True),
            ],
        ),
        _key_table_info(ReportTable.GENRE, _GENRE_LENGTH),
        _key_table_info(ReportTable.PROFESSION, _PROFESSION_LENGTH),
        _key_table_info(ReportTable.TITLE_TYPE, _ALIAS_TYPE_LENGTH),
        (
            ReportTable.NAME,
            [
                Column("id", Integer, nullable=False, primary_key=True),
                Column("nconst", String(_NCONST_LENGTH), nullable=False, unique=True),
                Column("primary_name", String(_TITLE_LENGTH), nullable=False),
                Column("birth_year", Integer),
                Column("death_year", Integer),
                Column("primary_professions", String((_PROFESSION_LENGTH + 1) * _PROFESSION_COUNT - 1)),
                Index("index__name__nconst", "nconst", unique=True),
            ],
        ),
        _ordered_relation_table_info(ReportTable.NAME_TO_KNOWN_FOR_TITLE, ReportTable.NAME, ReportTable.TITLE),
        (
            ReportTable.PARTICIPATION,
            [
                Column("id", Integer, nullable=False, primary_key=True),
                Column("title_id", Integer, ForeignKey("title.id"), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column("name_id", Integer, ForeignKey("name.id"), nullable=False),
                Column("profession_id", Integer, ForeignKey("profession.id")),
                Column("job", String(_JOB_LENGTH)),
                Index("index__participation__title_id__ordering", "title_id", "ordering", unique=True),
                Index("index__participation__name_id", "name_id"),
            ],
        ),
        _ordered_relation_table_info(
            ReportTable.PARTICIPATION_TO_CHARACTER, ReportTable.PARTICIPATION, ReportTable.CHARACTER
        ),
        (
            ReportTable.TITLE,
            [
                Column("id", Integer, nullable=False, primary_key=True),
                Column("tconst", String(_TCONST_LENGTH), nullable=False, unique=True),
                Column("title_type_id", Integer, ForeignKey("title_type.id"), nullable=False),
                Column("primary_title", String(_TITLE_LENGTH), nullable=False),
                Column("original_title", String(_TITLE_LENGTH), nullable=False),
                Column("is_adult", Boolean, nullable=False),
                Column("start_year", Integer),
                Column("end_year", Integer),
                Column("runtime_minutes", Integer),
                Column("average_rating", Float, default=0.0, nullable=False),
                Column("rating_count", Integer, default=0, nullable=False),
                Index("index__title__tconst", "tconst", unique=True),
            ],
        ),
        (
            ReportTable.TITLE_ALIAS,
            [
                Column("id", Integer, nullable=False, primary_key=True),
                Column("title_id", Integer, ForeignKey("title.id"), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column("title", String(_TITLE_LENGTH), nullable=False),
                Column("region_code", String(_REGION_LENGTH)),
                Column("language_code", String(_LANGUAGE_LENGTH)),
                # NOTE: is_original_title sometimes actually is null.
                Column("is_original_title", Boolean),
                Index("index__title_alias__title_id__ordering", "title_id", "ordering", unique=True),
            ],
        ),
        _ordered_relation_table_info(
            ReportTable.TITLE_ALIAS_TO_TITLE_ALIAS_TYPE, ReportTable.TITLE_ALIAS, ReportTable.TITLE_ALIAS_TYPE
        ),
        _key_table_info(ReportTable.TITLE_ALIAS_TYPE, _ALIAS_TYPE_LENGTH),
        _ordered_relation_table_info(ReportTable.TITLE_TO_DIRECTOR, ReportTable.TITLE, ReportTable.NAME),
        _ordered_relation_table_info(ReportTable.TITLE_TO_GENRE, ReportTable.TITLE, ReportTable.GENRE),
        _ordered_relation_table_info(ReportTable.TITLE_TO_WRITER, ReportTable.TITLE, ReportTable.NAME),
    ]


def typed_column_to_value_map(
    table: Table, column_name_to_raw_value_map: Dict[str, str]
) -> Dict[str, Optional[Union[bool, float, int, str]]]:
    result = {}
    for column in table.columns:
        raw_value = column_name_to_raw_value_map[column.name]
        column_python_type = column.type.python_type
        if raw_value == "\\N":
            value = None
            if not column.nullable:
                if column_python_type == bool:
                    value = False
                elif column_python_type in (float, int):
                    value = 0
                elif column_python_type == str:
                    value = ""
                else:
                    assert False, f"column_python_type={column_python_type}"
                log.warning(
                    'column "%s" of python type %s should not be null, using "%s" instead; raw_value_map=%s',
                    column.name,
                    column_python_type.__name__,
                    value,
                    column_name_to_raw_value_map,
                )
        elif column_python_type == bool:
            if raw_value == "1":
                value = True
            elif raw_value == "0":
                value = False
            else:
                raise PimdbError(f'value for column "{column.name}" must be a boolean but is: "{raw_value}"')
        else:
            value = column_python_type(raw_value)
        result[column.name] = value
    return result


class BulkInsert:
    def __init__(self, connection: Connection, table: Table, bulk_size: int = DEFAULT_BULK_SIZE):
        assert bulk_size >= 1
        self._connection = connection
        self._table = table
        self._bulk_size = bulk_size
        self._data = []
        self._count = 0
        self._start_time = time.time()

    def add(self, data: Dict[str, Optional[Any]]):
        self._data.append(data)
        self._count += 1
        if len(self._data) >= self._bulk_size:
            self._flush()

    def _flush(self):
        data_count = len(self._data)
        assert data_count >= 1
        log.debug("    inserting %d data to %s", data_count, self._table.name)
        insert = self._table.insert(self._data)
        self._connection.execute(insert)
        self._data.clear()

    @property
    def count(self):
        return self._count

    @property
    def duration_in_seconds(self):
        return time.time() - self._start_time

    def close(self):
        if len(self._data) >= 1:
            self._flush()
        duration_in_seconds = self.duration_in_seconds
        minutes, seconds = divmod(duration_in_seconds, 60)
        rows_per_second = 0 if duration_in_seconds == 0.0 else self.count / duration_in_seconds
        log.info(
            "  added %d rows in %s (%.2f rows per second)",
            self.count,
            f"{int(minutes):02d}:{seconds:06.3f}",
            rows_per_second,
        )
        self._data = None

    def __enter__(self):
        return self

    def __exit__(self, error_type, error_value, error_traceback):
        if not error_type:
            self.close()


def engined(engine_info_or_path: str) -> str:
    return engine_info_or_path if "://" in engine_info_or_path else f"sqlite:///{engine_info_or_path}"


class Database:
    def __init__(self, engine_info: str, bulk_size: int = DEFAULT_BULK_SIZE, has_to_drop_tables: bool = False):
        # FIXME: Remove possible username and password from logged engine info.
        actual_engine_info = engined(engine_info)
        log.info("connecting to database %s", actual_engine_info)
        self._engine = create_engine(actual_engine_info)
        self._bulk_size = bulk_size
        self._has_to_drop_tables = has_to_drop_tables
        self._metadata = MetaData(self._engine)
        self._imdb_dataset_to_table_map = None
        self._report_name_to_table_map = {}
        self._nconst_to_name_id_map = None

        #: Remembers title_alias_types that have yet to be added to IMDB_TITLE_ALIAS_TYPES.
        self._unknown_title_alias_types = None

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def metadata(self) -> MetaData:
        return self._metadata

    @property
    def imdb_dataset_to_table_map(self) -> Dict[ImdbDataset, Table]:
        assert self._imdb_dataset_to_table_map is not None, f"call {self.create_imdb_dataset_tables.__name__} first"
        return self._imdb_dataset_to_table_map

    def report_table_for(self, report_table_key: ReportTable) -> Table:
        return self._report_name_to_table_map[report_table_key]

    def _add_report_table(self, table: Table):
        self._report_name_to_table_map[ReportTable(table.name)] = table

    def connection(self) -> Connection:
        return self._engine.connect()

    def table_count(self, connection: Connection, table: Table) -> int:
        # TODO: Is there a way to use SQLAlchemy core queries for that?
        (result,) = connection.execute(text(f'select count(1) from "{table.name}"')).fetchone()
        return result

    def nconst_to_name_id_map(self, connection: Connection):
        if self._nconst_to_name_id_map is None:
            self._nconst_to_name_id_map = self._natural_key_to_id_map(connection, ReportTable.NAME, "nconst")
        return self._nconst_to_name_id_map

    def _natural_key_to_id_map(
        self, connection: Connection, report_table: ReportTable, natural_key_column: str = "name", id_column: str = "id"
    ) -> Dict[str, int]:
        table = self.report_table_for(report_table)
        log.info("  building mapping from %s.%s to %s.%s", table.name, natural_key_column, table.name, id_column)
        name_id_select = select([getattr(table.columns, natural_key_column), getattr(table.columns, id_column)])
        result = {name: id_ for name, id_ in connection.execute(name_id_select)}
        log.info("    found %d entries", len(result))
        return result

    def create_imdb_dataset_tables(self):
        log.info("creating imdb dataset tables")
        self._imdb_dataset_to_table_map = {
            table_name: Table(
                table_name.table_name, self.metadata, *columns, comment=f"IMDb dataset {table_name.filename}"
            )
            for table_name, columns in imdb_dataset_table_infos()
        }
        if self._has_to_drop_tables:
            self.metadata.drop_all()
        self.metadata.create_all()

    def build_all_dataset_tables(
        self, connection: Connection, dataset_folder: str, log_progress: Optional[Callable[[int, int], None]] = None
    ):
        for imdb_dataset_name in IMDB_DATASET_NAMES:
            self.build_dataset_table(connection, imdb_dataset_name, dataset_folder, log_progress)

    def build_dataset_table(
        self,
        connection: Connection,
        imdb_dataset_name: str,
        dataset_folder: str,
        log_progress: Optional[Callable[[int, int], None]] = None,
    ):
        imdb_dataset = ImdbDataset(imdb_dataset_name)
        table_to_modify = self.imdb_dataset_to_table_map[imdb_dataset]
        with connection.begin():
            # Clear the entire table.
            delete_statement = table_to_modify.delete()
            connection.execute(delete_statement)

            # Insert all rows from TSV.
            key_columns = self.key_columns(imdb_dataset)
            gzipped_tsv_path = os.path.join(dataset_folder, imdb_dataset.filename)
            gzipped_tsv_reader = GzippedTsvReader(gzipped_tsv_path, key_columns, log_progress)
            with BulkInsert(connection, table_to_modify, self._bulk_size) as bulk_insert:
                for raw_column_to_row_map in gzipped_tsv_reader.column_names_to_value_maps():
                    try:
                        bulk_insert.add(typed_column_to_value_map(table_to_modify, raw_column_to_row_map))
                    except PimdbError as error:
                        raise PimdbError(
                            f"{gzipped_tsv_path} ({gzipped_tsv_reader.row_number}): cannot process row: {error}"
                        )

    def create_report_tables(self):
        log.info("creating report tables")
        for report_table, options in report_table_infos():
            try:
                self._report_name_to_table_map[report_table] = Table(report_table.value, self.metadata, *options)
            except SQLAlchemyError as error:
                raise PimdbError(f'cannot create report table "{report_table.value}": {error}') from error
        if self._has_to_drop_tables:
            self.metadata.drop_all()
        self.metadata.create_all()

    def key_columns(self, imdb_dataset: ImdbDataset) -> Tuple:
        return tuple(
            column.name for column in self.imdb_dataset_to_table_map[imdb_dataset].columns if column.primary_key
        )

    def build_key_table_from_query(
        self, connection: Connection, report_table: ReportTable, query: SelectBase, delimiter: Optional[str] = None
    ):
        table_to_build = self.report_table_for(report_table)
        log.info("building key table: %s (from query)", table_to_build.name)
        single_line_query = " ".join(str(query).replace("\n", " ").split())
        log.debug("querying key values: %s", single_line_query)
        values = set()
        for (raw_value,) in connection.execute(query):
            if delimiter is None:
                values.add(raw_value)
            elif delimiter == "json":
                try:
                    values_from_json = json.loads(raw_value)
                except Exception as error:
                    raise PimdbError(f"cannot extract JSON from {raw_value!r}: {error}")
                if not isinstance(values_from_json, list):
                    raise PimdbError(f"JSON column must be a list but is: {raw_value!r}")
                values.update(values_from_json)
            else:
                values.update(raw_value.split(delimiter))
        self._build_key_table_from_values(connection, table_to_build, values)

    def build_key_table_from_values(self, connection: Connection, report_table: ReportTable, values: Sequence[str]):
        table_to_build = self.report_table_for(report_table)
        log.info("building key table: %s (from values)", report_table.value)
        self._build_key_table_from_values(connection, table_to_build, values)

    def _build_key_table_from_values(self, connection: Connection, table_to_build: Table, values: Sequence[str]):
        connection.execute(table_to_build.delete())
        with BulkInsert(connection, table_to_build, self._bulk_size) as bulk_insert:
            for value in sorted(values):
                bulk_insert.add({"name": value})
        self.check_table_has_data(connection, table_to_build)

    def build_title_alias_type_table(self, connection: Connection) -> None:
        with connection.begin():
            self.build_key_table_from_values(connection, ReportTable.TITLE_ALIAS_TYPE, IMDB_TITLE_ALIAS_TYPES)

    def build_title_type_table(self, connection: Connection):
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        with connection.begin():
            self.build_key_table_from_query(
                connection, ReportTable.TITLE_TYPE, select([title_basics_table.c.titleType]).distinct()
            )

    def build_genre_table(self, connection: Connection):
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        genres_column = title_basics_table.c.genres
        with connection.begin():
            self.build_key_table_from_query(
                connection, ReportTable.GENRE, select([genres_column]).where(genres_column.isnot(None)).distinct(), ","
            )

    def build_profession_table(self, connection: Connection):
        title_principals_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_PRINCIPALS]
        category_column = title_principals_table.c.category
        with connection.begin():
            self.build_key_table_from_query(
                connection, ReportTable.PROFESSION, select([category_column]).distinct(),
            )

    def build_participation_table(self, connection: Connection):
        participation_table = self.report_table_for(ReportTable.PARTICIPATION)
        self._log_building_table(participation_table)
        name_table = self.report_table_for(ReportTable.NAME)
        title_table = self.report_table_for(ReportTable.TITLE)
        title_principals_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_PRINCIPALS]
        profession_table = self.report_table_for(ReportTable.PROFESSION)

        with connection.begin():
            connection.execute(participation_table.delete())
            insert_participation = participation_table.insert().from_select(
                [
                    participation_table.c.title_id,
                    participation_table.c.ordering,
                    participation_table.c.name_id,
                    participation_table.c.profession_id,
                    participation_table.c.job,
                ],
                select(
                    [
                        title_table.c.id,
                        title_principals_table.c.ordering,
                        name_table.c.id,
                        profession_table.c.id,
                        title_principals_table.c.job,
                    ]
                ).select_from(
                    title_principals_table.join(name_table, name_table.c.nconst == title_principals_table.c.nconst)
                    .join(title_table, title_table.c.tconst == title_principals_table.c.tconst)
                    .join(profession_table, profession_table.c.name == title_principals_table.c.category)
                ),
            )
            connection.execute(insert_participation)
        self.check_table_count(connection, title_principals_table, participation_table, True)

    def build_characters_to_character_and_character_table(self, connection: Connection):
        characters_to_character_table = self.report_table_for(ReportTable.CHARACTERS_TO_CHARACTER)
        title_principals_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_PRINCIPALS]
        characters_column = title_principals_table.c.characters
        select_characters = select([characters_column]).where(characters_column.isnot(None)).distinct()
        character_count = 1
        # Add dummy character for participations that do not represent a character, for example director.
        character_name_to_character_id_map = {"": character_count}
        self._log_building_table(characters_to_character_table)
        with connection.begin():
            connection.execute(characters_to_character_table.delete())
            with BulkInsert(connection, characters_to_character_table, self._bulk_size) as bulk_insert:
                for (characters,) in connection.execute(select_characters):
                    try:
                        characters_names_from_json = json.loads(characters)
                    except Exception as error:
                        raise PimdbError(
                            f"cannot JSON parse {title_principals_table.name}.{characters_column.name}: "
                            f"{characters!r}: {error}"
                        )
                    if not isinstance(characters_names_from_json, list):
                        raise PimdbError(
                            f"{title_principals_table.name}.{characters_column.name} must be a JSON list but is: "
                            f"{characters!r}"
                        )
                    for ordering, character_name in enumerate(characters_names_from_json, start=1):
                        character_id = character_name_to_character_id_map.get(character_name)
                        if character_id is None:
                            character_count += 1
                            character_id = character_count
                            character_name_to_character_id_map[character_name] = character_id
                        bulk_insert.add({"characters": characters, "character_id": character_id, "ordering": ordering})

        character_table = self.report_table_for(ReportTable.CHARACTER)
        self._log_building_table(character_table)
        with connection.begin():
            connection.execute(character_table.delete())
            with BulkInsert(connection, character_table, self._bulk_size) as bulk_insert:
                for character_name, character_id in character_name_to_character_id_map.items():
                    bulk_insert.add({"id": character_id, "name": character_name})

    def build_participation_to_character_table(self, connection: Connection):
        participation_to_character_table = self.report_table_for(ReportTable.PARTICIPATION_TO_CHARACTER)
        self._log_building_table(participation_to_character_table)
        name_table = self.report_table_for(ReportTable.NAME)
        participation_table = self.report_table_for(ReportTable.PARTICIPATION)
        profession_table = self.report_table_for(ReportTable.PROFESSION)
        title_table = self.report_table_for(ReportTable.TITLE)
        title_principals_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_PRINCIPALS]
        characters_to_character = self.report_table_for(ReportTable.CHARACTERS_TO_CHARACTER)

        with connection.begin():
            connection.execute(participation_to_character_table.delete())
            insert_participation = participation_to_character_table.insert().from_select(
                [
                    participation_to_character_table.c.participation_id,
                    participation_to_character_table.c.ordering,
                    participation_to_character_table.c.character_id,
                ],
                select(
                    [
                        participation_table.c.id,
                        characters_to_character.c.ordering,
                        characters_to_character.c.character_id,
                    ]
                )
                .select_from(
                    participation_table.join(name_table, name_table.c.id == participation_table.c.name_id)
                    .join(title_table, title_table.c.id == participation_table.c.title_id)
                    .join(
                        title_principals_table,
                        and_(
                            title_principals_table.c.nconst == name_table.c.nconst,
                            title_principals_table.c.tconst == title_table.c.tconst,
                            title_principals_table.c.ordering == participation_table.c.ordering,
                        ),
                    )
                    .join(
                        characters_to_character,
                        characters_to_character.c.characters == title_principals_table.c.characters,
                    )
                    .join(profession_table, profession_table.c.name == title_principals_table.c.category)
                )
                .distinct(),
            )
            connection.execute(insert_participation)
        self.check_table_has_data(connection, participation_to_character_table, True)

    @staticmethod
    def _log_building_table(table: Table) -> None:
        log.info("building %s table", table.name)

    def build_name_table(self, connection: Connection) -> None:
        name_table = self.report_table_for(ReportTable.NAME)
        Database._log_building_table(name_table)
        name_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.NAME_BASICS]
        with connection.begin():
            delete_statement = name_table.delete().execution_options(autocommit=True)
            connection.execute(delete_statement)
            insert_statement = name_table.insert().from_select(
                [
                    name_table.c.nconst,
                    name_table.c.primary_name,
                    name_table.c.birth_year,
                    name_table.c.death_year,
                    name_table.c.primary_professions,
                ],
                select(
                    [
                        name_basics_table.c.nconst,
                        name_basics_table.c.primaryName,
                        name_basics_table.c.birthYear,
                        name_basics_table.c.deathYear,
                        name_basics_table.c.primaryProfession,
                    ]
                ),
            )
            connection.execute(insert_statement)

    def build_name_to_known_for_title_table(self, connection: Connection):
        name_to_known_for_title_table = self.report_table_for(ReportTable.NAME_TO_KNOWN_FOR_TITLE)
        Database._log_building_table(name_to_known_for_title_table)
        name_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.NAME_BASICS]
        name_table = self.report_table_for(ReportTable.NAME)
        known_for_titles_column = name_basics_table.c.knownForTitles
        select_known_for_title_tconsts = (
            select([name_table.c.id, name_table.c.nconst, known_for_titles_column])
            .select_from(name_table.join(name_basics_table, name_basics_table.c.nconst == name_table.c.nconst))
            .where(known_for_titles_column.isnot(None))
        )
        with connection.begin():
            tconst_to_title_id_map = self._natural_key_to_id_map(connection, ReportTable.TITLE, "tconst")
            connection.execute(name_to_known_for_title_table.delete())
            with BulkInsert(connection, name_to_known_for_title_table, self._bulk_size) as bulk_insert:
                for name_id, nconst, known_for_titles_tconsts in connection.execute(select_known_for_title_tconsts):
                    ordering = 0
                    for tconst in known_for_titles_tconsts.split(","):
                        title_id = tconst_to_title_id_map.get(tconst)
                        if title_id is not None:
                            ordering += 1
                            bulk_insert.add({"name_id": name_id, "ordering": ordering, "title_id": title_id})
                        else:
                            log.debug(
                                'ignored unknown %s.%s "%s" for name "%s"',
                                name_basics_table.name,
                                known_for_titles_column.name,
                                tconst,
                                nconst,
                            )

    def build_title_table(self, connection: Connection) -> None:
        title_table = self.report_table_for(ReportTable.TITLE)
        Database._log_building_table(title_table)
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        title_ratings_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_RATINGS]
        title_type_table = self.report_table_for(ReportTable.TITLE_TYPE)
        with connection.begin():
            delete_statement = title_table.delete().execution_options(autocommit=True)
            connection.execute(delete_statement)
            insert_statement = title_table.insert().from_select(
                [
                    title_table.c.tconst,
                    title_table.c.title_type_id,
                    title_table.c.primary_title,
                    title_table.c.original_title,
                    title_table.c.is_adult,
                    title_table.c.start_year,
                    title_table.c.end_year,
                    title_table.c.runtime_minutes,
                    title_table.c.average_rating,
                    title_table.c.rating_count,
                ],
                select(
                    [
                        title_basics_table.c.tconst,
                        title_type_table.c.id,
                        title_basics_table.c.primaryTitle,
                        title_basics_table.c.originalTitle,
                        title_basics_table.c.isAdult,
                        title_basics_table.c.startYear,
                        title_basics_table.c.endYear,
                        title_basics_table.c.runtimeMinutes,
                        coalesce(title_ratings_table.c.averageRating, 0),
                        coalesce(title_ratings_table.c.numVotes, 0),
                    ]
                ).select_from(
                    title_basics_table.join(
                        title_type_table, title_type_table.c.name == title_basics_table.c.titleType
                    ).join(
                        # Not all titles are rated so we need to use an outer join here.
                        title_ratings_table,
                        title_ratings_table.c.tconst == title_basics_table.c.tconst,
                        isouter=True,
                    )
                ),
            )
            connection.execute(insert_statement)
            self.check_table_count(connection, title_basics_table, title_table)

    def check_table_count(self, connection, source_table, target_table, log_count=False):
        source_table_count = self.table_count(connection, source_table)
        target_table_count = self.table_count(connection, target_table)
        if log_count:
            log.info('  added %d rows to "%s"', target_table_count, target_table.name)
        if target_table_count != source_table_count:
            log.warning(
                'target table "%s" has %d rows but should have %d same as source table "%s"',
                target_table.name,
                target_table_count,
                source_table_count,
                source_table.name,
            )

    def check_table_has_data(self, connection: Connection, target_table: Table, log_count: bool = False):
        target_table_count = self.table_count(connection, target_table)
        if log_count:
            log.info('  added %d rows to "%s"', target_table_count, target_table.name)
        if target_table_count == 0:
            log.warning('target table "%s" should contain rows but is empty',)

    def build_title_to_genre_table(self, connection: Connection):
        title_to_genre_table = self.report_table_for(ReportTable.TITLE_TO_GENRE)
        Database._log_building_table(title_to_genre_table)
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        title_table = self.report_table_for(ReportTable.TITLE)
        genres_column = title_basics_table.c.genres
        select_genre_data = (
            select([title_table.c.id, genres_column])
            .select_from(title_table.join(title_basics_table, title_basics_table.c.tconst == title_table.c.tconst))
            .where(genres_column.isnot(None))
        )
        genre_name_to_id_map = self._natural_key_to_id_map(connection, ReportTable.GENRE)
        with connection.begin():
            connection.execute(title_to_genre_table.delete())
            with BulkInsert(connection, title_to_genre_table, self._bulk_size) as bulk_insert:
                for title_id, genres in connection.execute(select_genre_data):
                    for ordering, genre in enumerate(genres.split(","), start=1):
                        genre_id = genre_name_to_id_map[genre]
                        bulk_insert.add({"genre_id": genre_id, "ordering": ordering, "title_id": title_id})

    def build_title_to_director_table(self, connection: Connection) -> None:
        title_to_director_table = self.report_table_for(ReportTable.TITLE_TO_DIRECTOR)
        self._build_title_to_crew_table(connection, "directors", title_to_director_table)

    def build_title_to_writer_table(self, connection: Connection) -> None:
        title_to_writer_table = self.report_table_for(ReportTable.TITLE_TO_WRITER)
        self._build_title_to_crew_table(connection, "writers", title_to_writer_table)

    def _build_title_to_crew_table(
        self, connection: Connection, column_with_nconsts_name: str, target_table: Table
    ) -> None:
        nconst_to_name_id_map = self.nconst_to_name_id_map(connection)
        self._log_building_table(target_table)
        title_table = self.report_table_for(ReportTable.TITLE)
        title_crew_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_CREW]
        column_with_nconsts = getattr(title_crew_table.columns, column_with_nconsts_name)
        with connection.begin():
            delete_statement = target_table.delete()
            connection.execute(delete_statement)
            directors_select = (
                select([title_table.c.id, title_table.c.tconst, column_with_nconsts])
                .select_from(title_table.join(title_crew_table, title_table.c.tconst == title_crew_table.c.tconst))
                .where(column_with_nconsts.isnot(None))
            )
            with BulkInsert(connection, target_table, self._bulk_size) as bulk_insert:
                for title_id, tconst, directors in connection.execute(directors_select):
                    ordering = 0
                    for nconst in directors.split(","):
                        name_id = nconst_to_name_id_map.get(nconst)
                        if name_id is not None:
                            ordering += 1
                            bulk_insert.add({"name_id": name_id, "ordering": ordering, "title_id": title_id})
                        else:
                            log.debug(
                                'ignored unknown %s.%s "%s" for title "%s"',
                                title_crew_table.name,
                                column_with_nconsts_name,
                                nconst,
                                tconst,
                            )

    @functools.lru_cache(None)
    def mappable_title_alias_types(self, raw_title_types: str) -> List[str]:
        # TODO: Make inner function of build_title_alias_to_title_alias_type_table().
        result = []
        if raw_title_types:
            remaining_raw_title_alias_types = raw_title_types
            for title_alias_type_to_check in IMDB_TITLE_ALIAS_TYPES:
                if title_alias_type_to_check in remaining_raw_title_alias_types:
                    result.append(title_alias_type_to_check)
                    remaining_raw_title_alias_types = remaining_raw_title_alias_types.replace(
                        title_alias_type_to_check, ""
                    )
            if (
                remaining_raw_title_alias_types
                and remaining_raw_title_alias_types not in self._unknown_title_alias_types
            ):
                self._unknown_title_alias_types.add(remaining_raw_title_alias_types)
                log.warning(
                    'cannot map %s.types "%s" to a known type: IMDB_TITLE_ALIAS_TYPES should be extended accordingly',
                    ImdbDataset.TITLE_AKAS.table_name,
                    remaining_raw_title_alias_types,
                )
        return result

    def build_title_alias_table(self, connection: Connection):
        title_alias_table = self.report_table_for(ReportTable.TITLE_ALIAS)
        Database._log_building_table(title_alias_table)
        title_akas_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_AKAS]
        title_table = self.report_table_for(ReportTable.TITLE)

        with connection.begin():
            connection.execute(title_alias_table.delete())
            insert_title_alias_table = title_alias_table.insert().from_select(
                [
                    title_alias_table.c.title_id,
                    title_alias_table.c.ordering,
                    title_alias_table.c.title,
                    title_alias_table.c.region_code,
                    title_alias_table.c.language_code,
                    title_alias_table.c.is_original_title,
                ],
                select(
                    [
                        title_table.c.id,
                        title_akas_table.c.ordering,
                        title_akas_table.c.title,
                        title_akas_table.c.region,  # TODO: use lower()
                        title_akas_table.c.language,  # TODO: use lower()
                        title_akas_table.c.isOriginalTitle,
                    ]
                ).select_from(title_table.join(title_akas_table, title_akas_table.c.titleId == title_table.c.tconst)),
            )
            connection.execute(insert_title_alias_table)
            self.check_table_has_data(connection, title_alias_table)

    def build_title_alias_to_title_alias_type_table(self, connection: Connection):
        title_alias_to_title_alias_type_table = self.report_table_for(ReportTable.TITLE_ALIAS_TO_TITLE_ALIAS_TYPE)
        Database._log_building_table(title_alias_to_title_alias_type_table)
        title_table = self.report_table_for(ReportTable.TITLE)
        title_akas_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_AKAS]
        title_alias_table = self.report_table_for(ReportTable.TITLE_ALIAS)
        title_alias_type_name_to_id_map = self._natural_key_to_id_map(connection, ReportTable.TITLE_ALIAS_TYPE)
        self._unknown_title_alias_types = set()

        title_akas_types_column = title_akas_table.c.types
        select_title_akas_data = (
            select([title_alias_table.c.id, title_akas_table.c.ordering, title_akas_types_column])
            .select_from(
                title_alias_table.join(title_table, title_table.c.id == title_alias_table.c.title_id).join(
                    title_akas_table,
                    and_(
                        title_akas_table.c.titleId == title_table.c.tconst,
                        title_akas_table.c.ordering == title_alias_table.c.ordering,
                    ),
                )
            )
            .where(title_akas_types_column.isnot(None))
        )
        with connection.begin():
            connection.execute(title_alias_to_title_alias_type_table.delete())
            with BulkInsert(connection, title_alias_to_title_alias_type_table, self._bulk_size) as bulk_insert:
                for (title_alias_id, title_alias_ordering, raw_title_alias_types,) in connection.execute(
                    select_title_akas_data
                ):
                    for title_alias_type_ordering, title_alias_type_name in enumerate(
                        self.mappable_title_alias_types(raw_title_alias_types), start=1
                    ):
                        title_alias_type_id = title_alias_type_name_to_id_map[title_alias_type_name]
                        bulk_insert.add(
                            {
                                "title_alias_id": title_alias_id,
                                "ordering": title_alias_type_ordering,
                                "title_alias_type_id": title_alias_type_id,
                            }
                        )
