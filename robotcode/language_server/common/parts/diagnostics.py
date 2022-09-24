from __future__ import annotations

import asyncio
import itertools
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from ....jsonrpc2.protocol import JsonRPCErrorException, rpc_method
from ....utils.async_tools import (
    Event,
    Lock,
    async_event,
    async_tasking_event,
    async_tasking_event_iterator,
    check_canceled,
    create_sub_task,
    threaded,
)
from ....utils.logging import LoggingDescriptor
from ....utils.uri import Uri
from ..decorators import language_id_filter
from ..has_extend_capabilities import HasExtendCapabilities
from ..lsp_types import (
    Diagnostic,
    DiagnosticOptions,
    DocumentDiagnosticParams,
    DocumentDiagnosticReport,
    ErrorCodes,
    PreviousResultId,
    ProgressToken,
    RelatedFullDocumentDiagnosticReport,
    RelatedUnchangedDocumentDiagnosticReport,
    ServerCapabilities,
    TextDocumentIdentifier,
    WorkspaceDiagnosticParams,
    WorkspaceDiagnosticReport,
)
from ..text_document import TextDocument

if TYPE_CHECKING:
    from ..protocol import LanguageServerProtocol

from .protocol_part import LanguageServerProtocolPart

__all__ = ["DiagnosticsProtocolPart", "DiagnosticsResult"]


class DiagnosticsMode(Enum):
    WORKSPACE = "workspace"
    OPENFILESONLY = "openFilesOnly"


class AnalysisProgressMode(Enum):
    SIMPLE = "simple"
    DETAILED = "detailed"


WORKSPACE_URI = Uri("workspace:/")


@dataclass
class DiagnosticsResult:
    key: Any
    diagnostics: Optional[List[Diagnostic]] = None


@dataclass
class WorkspaceDocumentsResult:
    name: Optional[str]
    document: TextDocument


@dataclass
class DiagnosticsData:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    entries: Dict[Any, Optional[List[Diagnostic]]] = field(default_factory=dict)
    version: Optional[int] = None
    task: Optional[asyncio.Task[Any]] = None
    force: bool = False


def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    to_cancel = asyncio.all_tasks(loop)
    if not to_cancel:
        return

    for task in to_cancel:
        task.cancel()

    loop.run_until_complete(asyncio.gather(*to_cancel, loop=loop, return_exceptions=True))

    for task in to_cancel:
        if task.cancelled():
            continue
        if task.exception() is not None:
            loop.call_exception_handler(
                {
                    "message": "unhandled exception during asyncio.run() shutdown",
                    "exception": task.exception(),
                    "task": task,
                }
            )


class DiagnosticsProtocolPart(LanguageServerProtocolPart, HasExtendCapabilities):
    _logger = LoggingDescriptor()

    def __init__(self, protocol: LanguageServerProtocol) -> None:
        super().__init__(protocol)

        self.workspace_loaded_event = Event()

        self._workspace_load_lock = Lock()
        self._workspace_loaded = False

        self._current_document_tasks: Dict[
            TextDocument, asyncio.Task[Optional[RelatedFullDocumentDiagnosticReport]]
        ] = {}

        self._current_workspace_task: Optional[asyncio.Task[WorkspaceDiagnosticReport]] = None

        self._diagnostics_loop: Optional[asyncio.AbstractEventLoop] = None
        self._diagnostics_loop_lock = threading.RLock()
        self._diagnostics_started = threading.Event()

        self.parent.on_initialized.add(self.initialized)

        self.in_get_workspace_diagnostics = Event(True)

    async def initialized(self, sender: Any) -> None:
        self._ensure_diagnostics_thread_started()

    @property
    def diagnostics_loop(self) -> asyncio.AbstractEventLoop:
        if self._diagnostics_loop is None:
            self._ensure_diagnostics_thread_started()

        assert self._diagnostics_loop is not None

        return self._diagnostics_loop

    def _run_diagnostics(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            self._diagnostics_loop = loop
            self._diagnostics_started.set()

            loop.slow_callback_duration = 10

            loop.run_forever()
            _cancel_all_tasks(loop)
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def _ensure_diagnostics_thread_started(self) -> None:
        with self._diagnostics_loop_lock:
            if self._diagnostics_loop is None:
                self._server_thread = threading.Thread(
                    name="diagnostics_worker", target=self._run_diagnostics, daemon=True
                )

                self._server_thread.start()

                if not self._diagnostics_started.wait(10):
                    raise RuntimeError("Can't start diagnostics worker thread.")

    def extend_capabilities(self, capabilities: ServerCapabilities) -> None:
        if (
            self.parent.client_capabilities is not None
            and self.parent.client_capabilities.text_document is not None
            and self.parent.client_capabilities.text_document.diagnostic is not None
        ):
            capabilities.diagnostic_provider = DiagnosticOptions(
                inter_file_dependencies=True,
                workspace_diagnostics=False,
                identifier=f"robotcodelsp_{uuid.uuid4()}",
                work_done_progress=True,
            )

    @async_tasking_event_iterator
    async def collect(sender, document: TextDocument) -> DiagnosticsResult:  # NOSONAR
        ...

    @async_tasking_event
    async def load_workspace_documents(sender) -> List[WorkspaceDocumentsResult]:  # NOSONAR
        ...

    @async_tasking_event
    async def on_workspace_loaded(sender) -> None:  # NOSONAR
        ...

    @async_tasking_event
    async def on_workspace_diagnostics_ended(sender) -> None:  # NOSONAR
        ...

    @async_tasking_event
    async def on_document_diagnostics_ended(sender) -> None:  # NOSONAR
        ...

    @async_event
    async def on_get_analysis_progress_mode(sender, uri: Uri) -> Optional[AnalysisProgressMode]:  # NOSONAR
        ...

    @async_event
    async def on_get_diagnostics_mode(sender, uri: Uri) -> Optional[DiagnosticsMode]:  # NOSONAR
        ...

    async def ensure_workspace_loaded(self) -> None:
        if not self._workspace_loaded:
            async with self._workspace_load_lock:
                if not self._workspace_loaded:
                    if self.workspace_loaded_event.is_set():
                        return

                    try:
                        await self.load_workspace_documents(self)
                    finally:
                        self._workspace_loaded = True
                        self.workspace_loaded_event.set()
                        await self.on_workspace_loaded(self)
                        await self.force_refresh_all()

    async def force_refresh_all(self) -> None:
        for doc in self.parent.documents.documents:
            self.get_diagnostics_data(doc).force = True

        await self.refresh()

    async def force_refresh_document(self, document: TextDocument) -> None:
        self.get_diagnostics_data(document).force = True
        if document.opened_in_editor:
            await self.refresh()

    @_logger.call
    async def _get_diagnostics_for_document(self, document: TextDocument, data: DiagnosticsData) -> None:
        self._logger.debug(lambda: f"Get diagnostics for {document}")

        await asyncio.sleep(0.75)

        collected_keys: List[Any] = []
        try:

            async for result_any in self.collect(
                self, document, callback_filter=language_id_filter(document), return_exceptions=True
            ):
                await check_canceled()

                if isinstance(result_any, BaseException):
                    if not isinstance(result_any, asyncio.CancelledError):
                        self._logger.exception(result_any)
                else:
                    result = cast(DiagnosticsResult, result_any)

                    data.id = str(uuid.uuid4())
                    data.entries[result.key] = result.diagnostics
                    if result.diagnostics is not None:
                        collected_keys.append(result.key)

                    if document.opened_in_editor:
                        await self.refresh()

        except asyncio.CancelledError:
            self._logger.debug(lambda: f"_get_diagnostics cancelled for {document}")
        finally:
            for k in set(data.entries.keys()) - set(collected_keys):
                data.entries.pop(k)

    @rpc_method(name="textDocument/diagnostic", param_type=DocumentDiagnosticParams)
    @threaded()
    async def _text_document_diagnostic(
        self,
        text_document: TextDocumentIdentifier,
        identifier: Optional[str],
        previous_result_id: Optional[str],
        *args: Any,
        **kwargs: Any,
    ) -> DocumentDiagnosticReport:
        try:
            document = await self.parent.documents.get(text_document.uri)
            if document is None:
                raise JsonRPCErrorException(ErrorCodes.SERVER_CANCELLED, f"Document {text_document!r} not found.")

            data = self.get_diagnostics_data(document)

            if data.force or document.version != data.version or data.task is None:

                task = data.task

                data = DiagnosticsData()
                document.set_data(self, data)

                if task is not None and not task.done():
                    self._logger.debug(lambda: f"try to cancel diagnostics for {document}")
                    task.get_loop().call_soon_threadsafe(task.cancel)

                data.version = document.version
                data.task = create_sub_task(
                    self._get_diagnostics_for_document(document, data),
                    loop=self.diagnostics_loop,
                    name=f"diagnostics ${text_document}",
                )

                def done(t: asyncio.Task[Any]) -> None:

                    self._logger.debug(lambda: f"diagnostics for {document} {'canceled' if t.cancelled() else 'ended'}")
                    try:
                        t.result()
                    except asyncio.CancelledError:
                        pass
                    except (SystemExit, KeyboardInterrupt):
                        raise
                    except BaseException as e:
                        self._logger.exception(e)

                data.task.add_done_callback(done)

            if data.id == previous_result_id:
                return RelatedUnchangedDocumentDiagnosticReport(result_id=data.id)

            return RelatedFullDocumentDiagnosticReport(
                list(itertools.chain(*(e for e in data.entries.values() if e is not None))), result_id=data.id
            )
        except asyncio.CancelledError:
            self._logger.debug("canceled _text_document_diagnostic")
            raise

    def get_diagnostics_data(self, document: TextDocument) -> DiagnosticsData:
        data: DiagnosticsData = document.get_data(self, None)

        if data is None:
            data = DiagnosticsData(str(uuid.uuid4()))
            document.set_data(self, data)

        return data

    @rpc_method(name="workspace/diagnostic", param_type=WorkspaceDiagnosticParams)
    @threaded()
    async def _workspace_diagnostic(
        self,
        identifier: Optional[str],
        previous_result_ids: List[PreviousResultId],
        partial_result_token: Optional[ProgressToken],
        work_done_token: Optional[ProgressToken] = None,
        *args: Any,
        **kwargs: Any,
    ) -> WorkspaceDiagnosticReport:
        self._logger.debug("workspace/diagnostic")

        # async def _get_diagnostics() -> WorkspaceDiagnosticReport:
        #     result: List[WorkspaceDocumentDiagnosticReport] = []

        #     for doc in self.parent.documents.documents:
        #         doc_result = await self.get_document_diagnostics(doc)

        #         if doc_result.result_id is not None and any(
        #             p
        #             for p in previous_result_ids
        #             if p.value == doc_result.result_id and Uri(p.uri).normalized() == doc.uri
        #         ):
        #             result.append(
        #                 WorkspaceUnchangedDocumentDiagnosticReport(doc.document_uri, doc.version,
        #                   doc_result.result_id)
        #             )
        #         else:
        #             result.append(
        #                 WorkspaceFullDocumentDiagnosticReport(
        #                     doc.document_uri, doc.version, doc_result.items, doc_result.result_id
        #                 )
        #             )
        #     return WorkspaceDiagnosticReport(items=result)

        # async def _get_partial_diagnostics() -> WorkspaceDiagnosticReport:
        #     async with self.parent.window.progress(
        #         "Analyse Workspace",
        #         progress_token=work_done_token,
        #         cancellable=False,
        #     ) as progress:

        #         async def _task(doc: TextDocument) -> None:
        #             if doc.opened_in_editor:
        #                 return

        #             if await self.get_analysis_progress_mode(doc.uri) == AnalysisProgressMode.DETAILED:
        #                 path = doc.uri.to_path()
        #                 folder = self.parent.workspace.get_workspace_folder(doc.uri)
        #                 if folder is None:
        #                     name = path
        #                 else:
        #                     name = path.relative_to(folder.uri.to_path())

        #                 progress.report(f"Analyse {name}")

        #             doc_result = await self.get_document_diagnostics(doc)

        #             if doc_result.result_id is not None and any(
        #                 p
        #                 for p in previous_result_ids
        #                 if p.value == doc_result.result_id and Uri(p.uri).normalized() == doc.uri
        #             ):
        #                 self.parent.window.send_progress(
        #                     partial_result_token,
        #                     WorkspaceDiagnosticReportPartialResult(
        #                         [
        #                             WorkspaceUnchangedDocumentDiagnosticReport(
        #                                 doc.document_uri, doc.version, doc_result.result_id
        #                             )
        #                         ]
        #                     ),
        #                 )
        #             else:
        #                 self.parent.window.send_progress(
        #                     partial_result_token,
        #                     WorkspaceDiagnosticReportPartialResult(
        #                         [
        #                             WorkspaceFullDocumentDiagnosticReport(
        #                                 doc.document_uri, doc.version, doc_result.items, doc_result.result_id
        #                             )
        #                         ]
        #                     ),
        #                 )

        #         for doc in self.parent.documents.documents:
        #             if await self.get_diagnostics_mode(doc.uri) == DiagnosticsMode.WORKSPACE:
        #                 await _task(doc)

        #         return WorkspaceDiagnosticReport(items=[])

        # self.in_get_workspace_diagnostics.clear()
        # try:
        #     await self.ensure_workspace_loaded()

        #     if self._current_workspace_task is not None:
        #         self._current_workspace_task.cancel()

        #     task = create_sub_task(_get_diagnostics() if partial_result_token is None else _get_partial_diagnostics())
        #     self._current_workspace_task = task
        #     try:
        #         return await task
        #     except asyncio.CancelledError:
        #         self._logger.debug("workspace/diagnostic canceled")
        #         if self._current_workspace_task is None:
        #             raise JsonRPCErrorException(
        #                 ErrorCodes.SERVER_CANCELLED,
        #                 "ServerCancelled",
        #                 data=DiagnosticServerCancellationData(True),
        #             )
        #         raise
        #     finally:
        #         if self._current_workspace_task == task:
        #             self._current_workspace_task = None
        #         self._logger.debug("workspace/diagnostic ready")
        # finally:
        #     self.in_get_workspace_diagnostics.set()
        #     await self.on_workspace_diagnostics_ended(self)

        # async def _get_diagnostics() -> WorkspaceDiagnosticReport:
        #     result: List[WorkspaceDocumentDiagnosticReport] = []

        #     for doc in self.parent.documents.documents:
        #         doc_result = await self.get_document_diagnostics(doc)

        #         if doc_result.result_id is not None and any(
        #             p
        #             for p in previous_result_ids
        #             if p.value == doc_result.result_id and Uri(p.uri).normalized() == doc.uri
        #         ):
        #             result.append(
        #                 WorkspaceUnchangedDocumentDiagnosticReport(doc.document_uri, doc.version,
        #                   doc_result.result_id)
        #             )
        #         else:
        #             result.append(
        #                 WorkspaceFullDocumentDiagnosticReport(
        #                     doc.document_uri, doc.version, doc_result.items, doc_result.result_id
        #                 )
        #             )
        #     return WorkspaceDiagnosticReport(items=result)

        # async def _get_partial_diagnostics() -> WorkspaceDiagnosticReport:
        #     async with self.parent.window.progress(
        #         "Analyse Workspace",
        #         progress_token=work_done_token,
        #         cancellable=False,
        #     ) as progress:

        #         async def _task(doc: TextDocument) -> None:
        #             if doc.opened_in_editor:
        #                 return

        #             if await self.get_analysis_progress_mode(doc.uri) == AnalysisProgressMode.DETAILED:
        #                 path = doc.uri.to_path()
        #                 folder = self.parent.workspace.get_workspace_folder(doc.uri)
        #                 if folder is None:
        #                     name = path
        #                 else:
        #                     name = path.relative_to(folder.uri.to_path())

        #                 progress.report(f"Analyse {name}")

        #             doc_result = await self.get_document_diagnostics(doc)

        #             if doc_result.result_id is not None and any(
        #                 p
        #                 for p in previous_result_ids
        #                 if p.value == doc_result.result_id and Uri(p.uri).normalized() == doc.uri
        #             ):
        #                 self.parent.window.send_progress(
        #                     partial_result_token,
        #                     WorkspaceDiagnosticReportPartialResult(
        #                         [
        #                             WorkspaceUnchangedDocumentDiagnosticReport(
        #                                 doc.document_uri, doc.version, doc_result.result_id
        #                             )
        #                         ]
        #                     ),
        #                 )
        #             else:
        #                 self.parent.window.send_progress(
        #                     partial_result_token,
        #                     WorkspaceDiagnosticReportPartialResult(
        #                         [
        #                             WorkspaceFullDocumentDiagnosticReport(
        #                                 doc.document_uri, doc.version, doc_result.items, doc_result.result_id
        #                             )
        #                         ]
        #                     ),
        #                 )

        #         for doc in self.parent.documents.documents:
        #             if await self.get_diagnostics_mode(doc.uri) == DiagnosticsMode.WORKSPACE:
        #                 await _task(doc)

        #
        return WorkspaceDiagnosticReport(items=[])

    async def get_analysis_progress_mode(self, uri: Uri) -> AnalysisProgressMode:
        for e in await self.on_get_analysis_progress_mode(self, uri):
            if e is not None:
                return cast(AnalysisProgressMode, e)

        return AnalysisProgressMode.SIMPLE

    async def get_diagnostics_mode(self, uri: Uri) -> DiagnosticsMode:
        for e in await self.on_get_diagnostics_mode(self, uri):
            if e is not None:
                return cast(DiagnosticsMode, e)

        return DiagnosticsMode.OPENFILESONLY

    async def refresh(self) -> None:
        if (
            self.parent.client_capabilities
            and self.parent.client_capabilities.workspace
            and self.parent.client_capabilities.workspace.diagnostics
            and self.parent.client_capabilities.workspace.diagnostics.refresh_support
        ):
            await self.parent.send_request_async("workspace/diagnostic/refresh")
