#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
import sys
from collections import defaultdict
from datetime import datetime
from operator import attrgetter
from time import time
from typing import List, Optional, Tuple, Union
from urllib.parse import quote

# Using `from elasticsearch import *` would break elasticsearch mocking used in unit test.
import elasticsearch
import pendulum
from elasticsearch_dsl import Search

from airflow.configuration import conf
from airflow.models import TaskInstance
from airflow.utils import timezone
from airflow.utils.log.file_task_handler import FileTaskHandler
from airflow.utils.log.json_formatter import JSONFormatter
from airflow.utils.log.logging_mixin import ExternalLoggingMixin, LoggingMixin

# Elasticsearch hosted log type
EsLogMsgType = List[Tuple[str, str]]


class ElasticsearchTaskHandler(FileTaskHandler, ExternalLoggingMixin, LoggingMixin):
    """
    ElasticsearchTaskHandler is a python log handler that
    reads logs from Elasticsearch. Note that Airflow does not handle the indexing
    of logs into Elasticsearch. Instead, Airflow flushes logs
    into local files. Additional software setup is required
    to index the logs into Elasticsearch, such as using
    Filebeat and Logstash.
    To efficiently query and sort Elasticsearch results, this handler assumes each
    log message has a field `log_id` consists of ti primary keys:
    `log_id = {dag_id}-{task_id}-{execution_date}-{try_number}`
    Log messages with specific log_id are sorted based on `offset`,
    which is a unique integer indicates log message's order.
    Timestamps here are unreliable because multiple log messages
    might have the same timestamp.
    """

    PAGE = 0
    MAX_LINE_PER_PAGE = 1000
    LOG_NAME = 'Elasticsearch'

    def __init__(
        self,
        base_log_folder: str,
        filename_template: str,
        log_id_template: str,
        end_of_log_mark: str,
        write_stdout: bool,
        json_format: bool,
        json_fields: str,
        host_field: str = "host",
        offset_field: str = "offset",
        host: str = "localhost:9200",
        frontend: str = "localhost:5601",
        es_kwargs: Optional[dict] = conf.getsection("elasticsearch_configs"),
    ):
        """
        :param base_log_folder: base folder to store logs locally
        :param log_id_template: log id template
        :param host: Elasticsearch host name
        """
        es_kwargs = es_kwargs or {}
        super().__init__(base_log_folder, filename_template)
        self.closed = False

        self.client = elasticsearch.Elasticsearch([host], **es_kwargs)

        self.log_id_template = log_id_template
        self.frontend = frontend
        self.mark_end_on_close = True
        self.end_of_log_mark = end_of_log_mark
        self.write_stdout = write_stdout
        self.json_format = json_format
        self.json_fields = [label.strip() for label in json_fields.split(",")]
        self.host_field = host_field
        self.offset_field = offset_field
        self.context_set = False

        self.formatter: logging.Formatter
        self.handler: Union[logging.FileHandler, logging.StreamHandler]  # type: ignore[assignment]

    def _render_log_id(self, ti: TaskInstance, try_number: int) -> str:
        dag_run = ti.dag_run

        if self.json_format:
            data_interval_start = self._clean_date(dag_run.data_interval_start)
            data_interval_end = self._clean_date(dag_run.data_interval_end)
            execution_date = self._clean_date(dag_run.execution_date)
        else:
            data_interval_start = dag_run.data_interval_start.isoformat()
            data_interval_end = dag_run.data_interval_end.isoformat()
            execution_date = dag_run.execution_date.isoformat()

        return self.log_id_template.format(
            dag_id=ti.dag_id,
            task_id=ti.task_id,
            run_id=ti.run_id,
            data_interval_start=data_interval_start,
            data_interval_end=data_interval_end,
            execution_date=execution_date,
            try_number=try_number,
        )

    @staticmethod
    def _clean_date(value: datetime) -> str:
        """
        Clean up a date value so that it is safe to query in elasticsearch
        by removing reserved characters.
        # https://www.elastic.co/guide/en/elasticsearch/reference/current/query-dsl-query-string-query.html#_reserved_characters

        :param execution_date: execution date of the dag run.
        """
        return value.strftime("%Y_%m_%dT%H_%M_%S_%f")

    def _group_logs_by_host(self, logs):
        grouped_logs = defaultdict(list)
        for log in logs:
            key = getattr(log, self.host_field, 'default_host')
            grouped_logs[key].append(log)

        # return items sorted by timestamp.
        result = sorted(grouped_logs.items(), key=lambda kv: getattr(kv[1][0], 'message', '_'))

        return result

    def _read_grouped_logs(self):
        return True

    def _read(
        self, ti: TaskInstance, try_number: int, metadata: Optional[dict] = None
    ) -> Tuple[EsLogMsgType, dict]:
        """
        Endpoint for streaming log.

        :param ti: task instance object
        :param try_number: try_number of the task instance
        :param metadata: log metadata,
                         can be used for steaming log reading and auto-tailing.
        :return: a list of tuple with host and log documents, metadata.
        """
        if not metadata:
            metadata = {'offset': 0}
        if 'offset' not in metadata:
            metadata['offset'] = 0

        offset = metadata['offset']
        log_id = self._render_log_id(ti, try_number)

        logs = self.es_read(log_id, offset, metadata)
        logs_by_host = self._group_logs_by_host(logs)

        next_offset = offset if not logs else attrgetter(self.offset_field)(logs[-1])

        # Ensure a string here. Large offset numbers will get JSON.parsed incorrectly
        # on the client. Sending as a string prevents this issue.
        # https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Number/MAX_SAFE_INTEGER
        metadata['offset'] = str(next_offset)

        # end_of_log_mark may contain characters like '\n' which is needed to
        # have the log uploaded but will not be stored in elasticsearch.
        loading_hosts = [
            item[0] for item in logs_by_host if item[-1][-1].message != self.end_of_log_mark.strip()
        ]
        metadata['end_of_log'] = False if not logs else len(loading_hosts) == 0

        cur_ts = pendulum.now()
        if 'last_log_timestamp' in metadata:
            last_log_ts = timezone.parse(metadata['last_log_timestamp'])

            # if we are not getting any logs at all after more than N seconds of trying,
            # assume logs do not exist
            if int(next_offset) == 0 and cur_ts.diff(last_log_ts).in_seconds() > 5:
                metadata['end_of_log'] = True
                missing_log_message = (
                    f"*** Log {log_id} not found in Elasticsearch. "
                    "If your task started recently, please wait a moment and reload this page. "
                    "Otherwise, the logs for this task instance may have been removed."
                )
                return [('', missing_log_message)], metadata
            if (
                # Assume end of log after not receiving new log for N min,
                cur_ts.diff(last_log_ts).in_minutes() >= 5
                # if max_offset specified, respect it
                or ('max_offset' in metadata and int(offset) >= int(metadata['max_offset']))
            ):
                metadata['end_of_log'] = True

        if int(offset) != int(next_offset) or 'last_log_timestamp' not in metadata:
            metadata['last_log_timestamp'] = str(cur_ts)

        # If we hit the end of the log, remove the actual end_of_log message
        # to prevent it from showing in the UI.
        def concat_logs(lines):
            log_range = (len(lines) - 1) if lines[-1].message == self.end_of_log_mark.strip() else len(lines)
            return '\n'.join(self._format_msg(lines[i]) for i in range(log_range))

        message = [(host, concat_logs(hosted_log)) for host, hosted_log in logs_by_host]

        return message, metadata

    def _format_msg(self, log_line):
        """Format ES Record to match settings.LOG_FORMAT when used with json_format"""
        # Using formatter._style.format makes it future proof i.e.
        # if we change the formatter style from '%' to '{' or '$', this will still work
        if self.json_format:
            try:

                return self.formatter._style.format(_ESJsonLogFmt(self.json_fields, **log_line.to_dict()))
            except Exception:
                pass

        # Just a safe-guard to preserve backwards-compatibility
        return log_line.message

    def es_read(self, log_id: str, offset: str, metadata: dict) -> list:
        """
        Returns the logs matching log_id in Elasticsearch and next offset.
        Returns '' if no log is found or there was an error.

        :param log_id: the log_id of the log to read.
        :param offset: the offset start to read log from.
        :param metadata: log metadata, used for steaming log download.
        """
        # Offset is the unique key for sorting logs given log_id.
        search = Search(using=self.client).query('match_phrase', log_id=log_id).sort(self.offset_field)

        search = search.filter('range', **{self.offset_field: {'gt': int(offset)}})
        max_log_line = search.count()
        if 'download_logs' in metadata and metadata['download_logs'] and 'max_offset' not in metadata:
            try:
                if max_log_line > 0:
                    metadata['max_offset'] = attrgetter(self.offset_field)(
                        search[max_log_line - 1].execute()[-1]
                    )
                else:
                    metadata['max_offset'] = 0
            except Exception:
                self.log.exception('Could not get current log size with log_id: %s', log_id)

        logs = []
        if max_log_line != 0:
            try:

                logs = search[self.MAX_LINE_PER_PAGE * self.PAGE : self.MAX_LINE_PER_PAGE].execute()
            except Exception:
                self.log.exception('Could not read log with log_id: %s', log_id)

        return logs

    def emit(self, record):
        if self.handler:
            record.offset = int(time() * (10 ** 9))
            self.handler.emit(record)

    def set_context(self, ti: TaskInstance) -> None:
        """
        Provide task_instance context to airflow task handler.

        :param ti: task instance object
        """
        self.mark_end_on_close = not ti.raw

        if self.json_format:
            self.formatter = JSONFormatter(
                fmt=self.formatter._fmt,
                json_fields=self.json_fields + [self.offset_field],
                extras={
                    'dag_id': str(ti.dag_id),
                    'task_id': str(ti.task_id),
                    'execution_date': self._clean_date(ti.execution_date),
                    'try_number': str(ti.try_number),
                    'log_id': self._render_log_id(ti, ti.try_number),
                },
            )

        if self.write_stdout:
            if self.context_set:
                # We don't want to re-set up the handler if this logger has
                # already been initialized
                return

            self.handler = logging.StreamHandler(stream=sys.__stdout__)
            self.handler.setLevel(self.level)
            self.handler.setFormatter(self.formatter)
        else:
            super().set_context(ti)
        self.context_set = True

    def close(self) -> None:
        # When application exit, system shuts down all handlers by
        # calling close method. Here we check if logger is already
        # closed to prevent uploading the log to remote storage multiple
        # times when `logging.shutdown` is called.
        if self.closed:
            return

        if not self.mark_end_on_close:
            self.closed = True
            return

        # Case which context of the handler was not set.
        if self.handler is None:
            self.closed = True
            return

        # Reopen the file stream, because FileHandler.close() would be called
        # first in logging.shutdown() and the stream in it would be set to None.
        if self.handler.stream is None or self.handler.stream.closed:  # type: ignore[attr-defined]
            self.handler.stream = self.handler._open()  # type: ignore[union-attr]

        # Mark the end of file using end of log mark,
        # so we know where to stop while auto-tailing.
        self.handler.stream.write(self.end_of_log_mark)

        if self.write_stdout:
            self.handler.close()
            sys.stdout = sys.__stdout__

        super().close()

        self.closed = True

    @property
    def log_name(self) -> str:
        """The log name"""
        return self.LOG_NAME

    def get_external_log_url(self, task_instance: TaskInstance, try_number: int) -> str:
        """
        Creates an address for an external log collecting service.

        :param task_instance: task instance object
        :param try_number: task instance try_number to read logs from.
        :return: URL to the external log collection service
        :rtype: str
        """
        log_id = self._render_log_id(task_instance, try_number)
        scheme = '' if '://' in self.frontend else 'https://'
        return scheme + self.frontend.format(log_id=quote(log_id))

    @property
    def supports_external_link(self) -> bool:
        """Whether we can support external links"""
        return bool(self.frontend)


class _ESJsonLogFmt:
    """Helper class to read ES Logs and re-format it to match settings.LOG_FORMAT"""

    # A separate class is needed because 'self.formatter._style.format' uses '.__dict__'
    def __init__(self, json_fields: List, **kwargs):
        for field in json_fields:
            self.__setattr__(field, '')
        self.__dict__.update(kwargs)
