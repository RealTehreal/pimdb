import os
from typing import Dict, List, Optional, Sequence, Tuple, Union, Callable

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
    UniqueConstraint,
    Index,
)
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.sql import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.functions import coalesce
from sqlalchemy.sql.selectable import SelectBase

from pimdb.common import log, ImdbDataset, PimdbError, ReportTable, GzippedTsvReader, IMDB_DATASET_NAMES

#: Default number of bulk data (for e.g. SQL insert) to be collected in memory before they are sent to the database.
DEFAULT_BULK_SIZE = 128

_TCONST_LENGTH = 12  # current maximum: 10
_NCONST_LENGTH = 12  # current maximum: 10
_TITLE_LENGTH = 512  # current maximum: 408
_NAME_LENGTH = 160  # current maximum: 105
_GENRE_LENGTH = 16
_GENRE_COUNT = 4
_PROFESSION_LENGTH = 32
_PROFESSION_COUNT = 3
_REGION_LENGTH = 4
_LANGUAGE_LENGTH = 4
_CREW_COUNT = 2048  # current maximum: 1180
_CATEGORY_LENGTH = 32  # current maximum: 19
_JOB_LENGTH = 512  # current maximum: 286
_CHARACTER_LENGTH = 512  # TODO: limit to reasonable maximum

#: The "title_akas.types" field is a mess.
_ALIAS_TYPES_LENGTH = 64

#: The "title_akas.attributes" field is a mess.
_ATTRIBUTES_LENGTH = 128

IMDB_ALIAS_TYPES = ["alternative", "dvd", "festival", "tv", "video", "working", "original", "imdbDisplay"]

_ALIAS_TYPE_LENGTH = max(len(item) for item in IMDB_ALIAS_TYPES)
_ALIAS_TYPES_LENGTH = sum(len(item) for item in IMDB_ALIAS_TYPES)

_TITLE_TYPE_LENGTH = 16  # TODO: document current maximum.


def imdb_dataset_table_infos() -> List[Tuple[ImdbDataset, List[Column]]]:
    """SQL tables that represent a direct copy of a TSV file (excluding duplicates)"""
    return [
        (
            ImdbDataset.TITLE_BASICS,
            [
                Column("tconst", String(_TCONST_LENGTH), nullable=False, primary_key=True),
                Column("titleType", String(_TITLE_TYPE_LENGTH)),
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
                Column("knownForTitles", String((_TCONST_LENGTH + 1) * 4 - 1)),
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
                Column("category", String(_CATEGORY_LENGTH)),
                Column("job", String(_JOB_LENGTH)),
                Column("characters", String),
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


def _key_table_info(report_table: ReportTable, name_length: int) -> Tuple[ReportTable, List[Column]]:
    assert isinstance(report_table, ReportTable)
    return (
        report_table,
        [
            Column("id", Integer, nullable=False, primary_key=True),
            Column("name", String(name_length), nullable=False),
            Index(f"index__{report_table.value}__name", "name", unique=True),
        ],
    )


def report_table_infos() -> List[Tuple[ReportTable, List[Column]]]:
    return [
        _key_table_info(ReportTable.ALIAS_TYPE, _ALIAS_TYPE_LENGTH),
        _key_table_info(ReportTable.CATEGORY, _CATEGORY_LENGTH),
        _key_table_info(ReportTable.CHARACTER, _CHARACTER_LENGTH),
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
                Index("index__name__nconst", "nconst", unique=True),
            ],
        ),
        (
            ReportTable.NAME_TO_KNOWN_FOR_TITLE,
            [
                Column("name_id", Integer, ForeignKey("name.id"), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column("title_id", Integer, ForeignKey("title.id"), nullable=False),
                UniqueConstraint("name_id", "ordering"),
                Index("index__name_to_known_for_title__name_id", "name_id"),
                Index("index__name_to_known_for_title__title_id", "title_id"),
            ],
        ),
        (
            ReportTable.NAME_TO_PROFESSION,
            [
                Column("name_id", Integer, ForeignKey("name.id"), nullable=False),
                Column("profession_id", Integer, ForeignKey("profession.id"), nullable=False),
                UniqueConstraint("name_id", "profession_id"),
                Index("index__name_to_profession__name_id", "name_id"),
                Index("index__name_to_profession__profession_id", "profession_id"),
            ],
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
            ReportTable.TITLE_TO_DIRECTOR,
            [
                Column("title_id", Integer, ForeignKey("title.id"), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column("name_id", Integer, ForeignKey("name.id"), nullable=False),
                Index("index__title_to_director__title_id", "title_id", "ordering", unique=True),
                Index("index__title_to_director__name_id", "name_id"),
            ],
        ),
        (
            ReportTable.TITLE_TO_PRINCIPAL,
            [
                Column("title_id", Integer, ForeignKey("title.id"), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column("name_id", Integer, ForeignKey("name.id"), nullable=False),
                Column("category_id", Integer, ForeignKey("category.id"), nullable=False),
                Column("job", String(_JOB_LENGTH), nullable=False),
                Index(
                    "index__title_to_principal__title_id__name_id__ordering",
                    "title_id",
                    "name_id",
                    "ordering",
                    unique=True,
                ),
            ],
        ),
        (
            ReportTable.TITLE_TO_WRITER,
            [
                Column("title_id", Integer, ForeignKey("title.id"), nullable=False),
                Column("ordering", Integer, nullable=False),
                Column("name_id", Integer, ForeignKey("name.id"), nullable=False),
                Index("index__title_to_writer__title_id", "title_id", "ordering", unique=True),
                Index("index__title_to_writer__name_id", "name_id"),
            ],
        ),
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


class Database:
    def __init__(self, engine_info: str, bulk_size: int = DEFAULT_BULK_SIZE):
        # FIXME: Remove possible username and password from logged engine info.
        log.info("connecting to database %s", engine_info)
        self._engine = create_engine(engine_info)
        self._bulk_size = bulk_size
        self._metadata = MetaData(self._engine)
        self._imdb_dataset_to_table_map = None
        self._report_name_to_table_map = {}
        self._nconst_to_name_id_map = None
        self._tconst_to_title_id_map = None
        self._unknown_type_id = None
        self._data_to_insert = None

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

    @property
    def unknown_type_id(self) -> int:
        assert self._unknown_type_id is not None, f"{self.build_alias_type_table.__name__}() must be called first"
        return self._unknown_type_id

    def report_table_for(self, report_table_key: ReportTable) -> Table:
        return self._report_name_to_table_map[report_table_key]

    def _add_report_table(self, table: Table):
        self._report_name_to_table_map[ReportTable(table.name)] = table

    def connection(self) -> Connection:
        return self._engine.connect()

    def nconst_to_name_id_map(self, connection: Connection):
        if self._nconst_to_name_id_map is None:
            log.info("building mapping for nconst to name_id")
            name_table = self.report_table_for(ReportTable.NAME)
            name_select = select([name_table.c.id, name_table.c.nconst])
            self._nconst_to_name_id_map = {nconst: name_id for name_id, nconst in connection.execute(name_select)}
        return self._nconst_to_name_id_map

    def tconst_to_title_id_map(self, connection: Connection):
        if self._tconst_to_title_id_map is None:
            log.info("building mapping for tconst to title_id")
            title_table = self.report_table_for(ReportTable.TITLE)
            title_select = select([title_table.c.id, title_table.c.tconst])
            self._tconst_to_title_id_map = {tconst: title_id for title_id, tconst in connection.execute(title_select)}
        return self._tconst_to_title_id_map

    def create_imdb_dataset_tables(self):
        log.info("creating imdb dataset tables")
        self._imdb_dataset_to_table_map = {
            table_name: Table(table_name.value, self.metadata, *columns)
            for table_name, columns in imdb_dataset_table_infos()
        }
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
        # Clear the entire table.
        delete_statement = table_to_modify.delete().execution_options(autocommit=True)
        connection.execute(delete_statement)
        # Insert all rows from TSV.
        key_columns = self.key_columns(imdb_dataset)
        gzipped_tsv_path = os.path.join(dataset_folder, imdb_dataset.filename)
        gzipped_tsv_reader = GzippedTsvReader(gzipped_tsv_path, key_columns, log_progress)
        self._data_to_insert = []
        for raw_column_to_row_map in gzipped_tsv_reader.column_names_to_value_maps():
            try:
                self._data_to_insert.append(typed_column_to_value_map(table_to_modify, raw_column_to_row_map))
            except PimdbError as error:
                raise PimdbError(f"{gzipped_tsv_path} ({gzipped_tsv_reader.row_number}): cannot process row: {error}")
            self._checked_insert(connection, table_to_modify, False)
        self._checked_insert(connection, table_to_modify, True)

    def _checked_insert(self, connection, table, force: bool):
        data_count = len(self._data_to_insert)
        if (force and data_count >= 1) or (data_count >= self._bulk_size):
            with connection.begin() as transaction:
                connection.execute(table.insert(), self._data_to_insert)
                transaction.commit()
                self._data_to_insert.clear()
        if force:
            self._data_to_insert = None

    def create_report_tables(self):
        log.info("creating report tables")
        for report_table, options in report_table_infos():
            try:
                self._report_name_to_table_map[report_table] = Table(report_table.value, self.metadata, *options)
            except SQLAlchemyError as error:
                raise PimdbError(f'cannot create report table "{report_table.value}": {error}') from error
        self.metadata.create_all()

    def key_columns(self, imdb_dataset: ImdbDataset) -> Tuple:
        return tuple(
            column.name for column in self.imdb_dataset_to_table_map[imdb_dataset].columns if column.primary_key
        )

    def build_key_table_from_query(
        self, connection: Connection, report_table: ReportTable, query: SelectBase, delimiter: Optional[str] = None
    ) -> Table:
        table_to_build = self.report_table_for(report_table)
        log.info("building key table: %s (from query)", table_to_build.name)
        single_line_query = " ".join(str(query).replace("\n", " ").split())
        log.debug("querying key values: %s", single_line_query)
        values = set()
        for (raw_value,) in connection.execute(query):
            if delimiter is None:
                values.add(raw_value)
            else:
                values.update(raw_value.split(delimiter))
        self._build_key_table_from_values(connection, table_to_build, values)

    def build_key_table_from_values(
        self, connection: Connection, report_table: ReportTable, values: Sequence[str]
    ) -> Table:
        table_to_build = self.report_table_for(report_table)
        log.info("building key table: %s (from values)", report_table.value)
        self._build_key_table_from_values(connection, table_to_build, values)

    def _build_key_table_from_values(
        self, connection: Connection, table_to_build: Table, values: Sequence[str]
    ) -> Table:
        with connection.begin():
            connection.execute(table_to_build.delete())
            for value in sorted(values):
                connection.execute(table_to_build.insert(), name=value)
        return table_to_build

    def build_alias_type_table(self, connection: Connection) -> None:
        with connection.begin():
            self.build_key_table_from_values(connection, ReportTable.ALIAS_TYPE, IMDB_ALIAS_TYPES)
            alias_type_table = self.report_table_for(ReportTable.ALIAS_TYPE)
            connection.execute(alias_type_table.insert({"name": "unknown"}))
        (self._unknown_type_id,) = connection.execute(
            select([alias_type_table.c.id]).where(alias_type_table.c.name == "unknown")
        ).fetchone()
        assert self.unknown_type_id is not None

    def build_title_type_table(self, connection: Connection):
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        self.build_key_table_from_query(
            connection, ReportTable.TITLE_TYPE, select([title_basics_table.c.titleType]).distinct()
        )

    def build_genre_table(self, connection: Connection):
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        genres_column = title_basics_table.c.genres
        self.build_key_table_from_query(
            connection, ReportTable.GENRE, select([genres_column]).where(genres_column.isnot(None)).distinct()
        )

    def build_profession_table(self, connection: Connection):
        name_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.NAME_BASICS]
        primary_profession_column = name_basics_table.c.primaryProfession
        self.build_key_table_from_query(
            connection,
            ReportTable.PROFESSION,
            select([primary_profession_column]).where(primary_profession_column.isnot(None)).distinct(),
            ",",
        )

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
                [name_table.c.nconst, name_table.c.primary_name, name_table.c.birth_year, name_table.c.death_year],
                select(
                    [
                        name_basics_table.c.nconst,
                        name_basics_table.c.primaryName,
                        name_basics_table.c.birthYear,
                        name_basics_table.c.deathYear,
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
            tconst_to_title_id_map = self.tconst_to_title_id_map(connection)
            connection.execute(name_to_known_for_title_table.delete())
            for name_id, nconst, known_for_titles_tconsts in connection.execute(select_known_for_title_tconsts):
                ordering = 0
                for tconst in known_for_titles_tconsts.split(","):
                    title_id = tconst_to_title_id_map.get(tconst)
                    if title_id is not None:
                        ordering += 1
                        connection.execute(
                            name_to_known_for_title_table.insert(
                                {"name_id": name_id, "ordering": ordering, "title_id": title_id}
                            )
                        )
                    else:
                        log.warning(
                            'ignored unknown %s.%s "%s" for name "%s"',
                            name_basics_table.name,
                            known_for_titles_column.name,
                            tconst,
                            nconst,
                        )

    def build_name_to_profession_table(self, connection: Connection) -> None:
        log.info("building name_to_profession table")
        # name_table = self.report_table_for("name")
        # profession_table = self.report_table_for("profession")
        # TODO

    def build_title_table(self, connection: Connection) -> None:
        log.info("building title table")
        unknown_type_id = self.unknown_type_id
        title_table = self.report_table_for(ReportTable.TITLE)
        title_basics_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_BASICS]
        title_ratings_table = self.imdb_dataset_to_table_map[ImdbDataset.TITLE_RATINGS]
        type_table = self.report_table_for(ReportTable.TITLE_TYPE)
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
                        coalesce(type_table.c.id, unknown_type_id),
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
                        title_ratings_table, title_ratings_table.c.tconst == title_basics_table.c.tconst, isouter=True
                    ).join(type_table, type_table.c.name == title_basics_table.c.titleType, isouter=True)
                ),
            )
            connection.execute(insert_statement)

    def build_title_to_director_table(self, connection: Connection) -> None:
        title_to_director_table = self.report_table_for(ReportTable.TITLE_TO_DIRECTOR)
        self._build_title_to_crew_table(connection, "directors", title_to_director_table)

    def build_title_to_writer_table(self, connection: Connection) -> None:
        title_to_director_table = self.report_table_for(ReportTable.TITLE_TO_WRITER)
        self._build_title_to_crew_table(connection, "writers", title_to_director_table)

    def _build_title_to_crew_table(
        self, connection: Connection, column_with_nconsts_name: str, target_table: Table
    ) -> None:
        nconst_to_name_id_map = self.nconst_to_name_id_map(connection)
        log.info("building %s table", target_table.name)
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

            for title_id, tconst, directors in connection.execute(directors_select):
                ordering = 0
                for director_nconst in directors.split(","):
                    director_name_id = nconst_to_name_id_map.get(director_nconst)
                    if director_name_id is not None:
                        ordering += 1
                        target_table.insert({"name_id": director_name_id, "ordering": ordering, "title_id": title_id})
                    else:
                        log.warning(
                            'ignored unknown %s.%s "%s" for title "%s"',
                            title_crew_table.name,
                            column_with_nconsts_name,
                            director_nconst,
                            tconst,
                        )
