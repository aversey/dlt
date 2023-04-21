from collections import defaultdict
from typing import Callable, Tuple, Iterable, Optional, Any, cast, List, Iterator, Dict, Union, TypedDict
from itertools import chain

# from jsonpath_ng import parse as jsonpath_parse, JSONPath
from dlt.common.jsonpath import resolve_paths, TAnyJsonPath, compile_paths

from dlt.common.exceptions import TerminalException
from dlt.common.schema.utils import get_child_tables, group_tables_by_resource, compile_simple_regexes
from dlt.common.schema.typing import TSimpleRegex
from dlt.common.typing import REPattern

from dlt.pipeline.exceptions import PipelineStepFailed, PipelineHasPendingDataException
from dlt.pipeline.typing import TPipelineStep
from dlt.pipeline import Pipeline
from dlt.common.pipeline import TSourceState, _reset_resource_state, sources_state, _delete_source_state_keys, _get_matching_resources


def retry_load(retry_on_pipeline_steps: Tuple[TPipelineStep, ...] = ("load",)) -> Callable[[Exception], bool]:
    """A retry strategy for Tenacity that, with default setting, will repeat `load` step for all exceptions that are not terminal

    Use this condition with tenacity `retry_if_exception`. Terminal exceptions are exceptions that will not go away when operations is repeated.
    Examples: missing configuration values, Authentication Errors, terminally failed jobs exceptions etc.

    >>> data = source(...)
    >>> for attempt in Retrying(stop=stop_after_attempt(3), retry=retry_if_exception(retry_load(())), reraise=True):
    >>>     with attempt:
    >>>         p.run(data)

    Args:
        retry_on_pipeline_steps (Tuple[TPipelineStep, ...], optional): which pipeline steps are allowed to be repeated. Default: "load"

    """
    def _retry_load(ex: Exception) -> bool:
        # do not retry in normalize or extract stages
        if isinstance(ex, PipelineStepFailed) and ex.step not in retry_on_pipeline_steps:
            return False
        # do not retry on terminal exceptions
        if isinstance(ex, TerminalException) or (ex.__context__ is not None and isinstance(ex.__context__, TerminalException)):
            return False
        return True

    return _retry_load


class _DropInfo(TypedDict):
    tables: List[str]
    resource_states: List[str]
    resource_names: List[str]
    state_paths: List[str]
    schema_name: str
    dataset_name: str
    drop_all: bool
    resource_pattern: Optional[REPattern]


class DropCommand:
    def __init__(
        self,
        pipeline: Pipeline,
        resources: Union[Iterable[Union[str, TSimpleRegex]], Union[str, TSimpleRegex]] = (),
        schema_name: Optional[str] = None,
        state_paths: TAnyJsonPath = (),
        drop_all: bool = False,
        skip_state_wipe: bool = False
    ) -> None:
        self.pipeline = pipeline
        if isinstance(resources, str):
            resources = [resources]
        if isinstance(state_paths, str):
            state_paths = [state_paths]

        self.schema = pipeline.schemas[schema_name or pipeline.default_schema_name].clone()
        self.schema_tables = self.schema.tables
        self.drop_tables = self.drop_state = True

        resources = set(resources)
        resource_names = []
        if resources:
            self.resource_pattern = compile_simple_regexes(TSimpleRegex(r) for r in resources)
            resource_tables = group_tables_by_resource(self.schema_tables, pattern=self.resource_pattern)
            if self.drop_tables:
                self.tables_to_drop = list(chain.from_iterable(resource_tables.values()))
                self.tables_to_drop.reverse()
            resource_names = list(resource_tables.keys())
        else:
            self.resource_pattern = None
            self.tables_to_drop = []
            self.drop_tables = False  # No tables to drop

        self.skip_state_wipe = skip_state_wipe or not self.resource_pattern

        self.state_paths_to_drop = compile_paths(state_paths)
        self.drop_all = drop_all
        self.info: _DropInfo = dict(
            tables=[t['name'] for t in self.tables_to_drop], resource_states=[], state_paths=[],
            resource_names=resource_names,
            schema_name=self.schema.name, dataset_name=self.pipeline.dataset_name,
            drop_all=drop_all,
            resource_pattern=self.resource_pattern
        )
        self._new_state = self._create_modified_state()
        if self.skip_state_wipe and not self.state_paths_to_drop:
            self.drop_state = False

    def _drop_destination_tables(self) -> None:
        with self.pipeline._get_destination_client(self.schema) as client:
            client.drop_tables(*[tbl['name'] for tbl in self.tables_to_drop])

    def _delete_pipeline_tables(self) -> None:
        for tbl in self.tables_to_drop:
            del self.schema_tables[tbl['name']]
        self.schema.bump_version()

    def _list_state_paths(self, source_state: Dict[str, Any]) -> List[str]:
        return resolve_paths(self.state_paths_to_drop, source_state)

    def _create_modified_state(self) -> Dict[str, Any]:
        state: TSourceState = self.pipeline.state  # type: ignore[assignment]
        if not self.drop_state:
            return state  # type: ignore[return-value]
        source_states = sources_state(state).items()
        for source_name, source_state in source_states:
            if not self.skip_state_wipe:
                for key in _get_matching_resources(self.resource_pattern, source_state):
                    self.info['resource_states'].append(key)
                    _reset_resource_state(key, source_state)
            resolved_paths = resolve_paths(self.state_paths_to_drop, source_state)
            _delete_source_state_keys(resolved_paths, source_state)
            self.info['state_paths'].extend(f"{source_name}.{p}" for p in resolved_paths)
        return state  # type: ignore[return-value]

    def _drop_state_keys(self) -> None:
        state: Dict[str, Any]
        with self.pipeline.managed_state(extract_state=True, extract_unchanged=True) as state:  # type: ignore[assignment]
            state.clear()
            state.update(self._new_state)

    def _drop_all(self) -> None:
        with self.pipeline.sql_client(self.schema.name) as client:
            client.drop_dataset()
        self.pipeline.drop()
        self.pipeline.sync_destination()

    def __call__(self) -> None:
        if self.pipeline.has_pending_data:  # Raise when there are pending extracted/load files to prevent conflicts
            raise PipelineHasPendingDataException(self.pipeline.pipeline_name, self.pipeline.pipelines_dir)
        if self.drop_all:
            self._drop_all()
            return
        if not self.drop_state and not self.drop_tables:
            return  # Nothing to drop

        if self.drop_tables:
            self._delete_pipeline_tables()
            self._drop_destination_tables()
        if self.drop_state:
            self._drop_state_keys()
        if self.drop_tables:
            self.pipeline.schemas.save_schema(self.schema)
        # Send updated state to destination
        self.pipeline.normalize()
        try:
            self.pipeline.load(raise_on_failed_jobs=True)
        except Exception:
            # Clear extracted state on failure so command can run again
            self.pipeline._get_load_storage().wipe_normalized_packages()
            raise


def drop(
    pipeline: Pipeline,
    resources: Union[Iterable[str], str] = (),
    schema_name: str = None,
    state_paths: TAnyJsonPath = (),
    drop_all: bool = False,
    skip_state_wipe: bool = False,
) -> None:
    return DropCommand(pipeline, resources, schema_name, state_paths, drop_all, skip_state_wipe)()
