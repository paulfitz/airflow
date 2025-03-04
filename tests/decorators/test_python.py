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
import sys
from collections import namedtuple
from datetime import date, datetime, timedelta
from typing import Dict  # noqa: F401  # This is used by annotation tests.
from typing import Tuple

import pytest

from airflow.decorators import task as task_decorator
from airflow.decorators.base import DecoratedMappedOperator
from airflow.exceptions import AirflowException
from airflow.models import DAG
from airflow.models.mappedoperator import MappedOperator
from airflow.models.xcom_arg import XComArg
from airflow.utils import timezone
from airflow.utils.state import State
from airflow.utils.task_group import TaskGroup
from airflow.utils.types import DagRunType
from tests.operators.test_python import Call, assert_calls_equal, build_recording_function
from tests.test_utils.db import clear_db_runs

DEFAULT_DATE = timezone.datetime(2016, 1, 1)
END_DATE = timezone.datetime(2016, 1, 2)
INTERVAL = timedelta(hours=12)
FROZEN_NOW = timezone.datetime(2016, 1, 2, 12, 1, 1)

TI_CONTEXT_ENV_VARS = [
    'AIRFLOW_CTX_DAG_ID',
    'AIRFLOW_CTX_TASK_ID',
    'AIRFLOW_CTX_EXECUTION_DATE',
    'AIRFLOW_CTX_DAG_RUN_ID',
]


class TestAirflowTaskDecorator:
    def setup_class(self):
        clear_db_runs()

    def setup_method(self):
        self.dag = DAG("test_dag", default_args={"owner": "airflow", "start_date": DEFAULT_DATE})
        self.run = False

    def teardown_method(self):
        self.dag.clear()
        self.run = False
        clear_db_runs()

    def test_python_operator_python_callable_is_callable(self):
        """Tests that @task will only instantiate if
        the python_callable argument is callable."""
        not_callable = {}
        with pytest.raises(TypeError):
            task_decorator(not_callable, dag=self.dag)

    @pytest.mark.parametrize(
        "resolve",
        [
            pytest.param(eval, id="eval"),
            pytest.param(lambda t: t, id="stringify"),
        ],
    )
    @pytest.mark.parametrize(
        "annotation",
        [
            "dict",
            pytest.param(
                "dict[str, int]",
                marks=pytest.mark.skipif(
                    sys.version_info < (3, 9),
                    reason="PEP 585 is implemented in Python 3.9",
                ),
            ),
            "Dict",
            "Dict[str, int]",
        ],
    )
    def test_infer_multiple_outputs_using_dict_typing(self, resolve, annotation):
        @task_decorator
        def identity_dict(x: int, y: int) -> resolve(annotation):
            return {"x": x, "y": y}

        assert identity_dict(5, 5).operator.multiple_outputs is True

    def test_infer_multiple_outputs_using_other_typing(self):
        @task_decorator
        def identity_tuple(x: int, y: int) -> Tuple[int, int]:
            return x, y

        assert identity_tuple(5, 5).operator.multiple_outputs is False

        @task_decorator
        def identity_int(x: int) -> int:
            return x

        assert identity_int(5).operator.multiple_outputs is False

        @task_decorator
        def identity_notyping(x: int):
            return x

        assert identity_notyping(5).operator.multiple_outputs is False

    def test_manual_multiple_outputs_false_with_typings(self):
        @task_decorator(multiple_outputs=False)
        def identity2(x: int, y: int) -> Tuple[int, int]:
            return x, y

        with self.dag:
            res = identity2(8, 4)

        dr = self.dag.create_dagrun(
            run_id=DagRunType.MANUAL.value,
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING,
        )

        res.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        ti = dr.get_task_instances()[0]

        assert res.operator.multiple_outputs is False
        assert ti.xcom_pull() == [8, 4]
        assert ti.xcom_pull(key="return_value_0") is None
        assert ti.xcom_pull(key="return_value_1") is None

    def test_multiple_outputs_ignore_typing(self):
        @task_decorator
        def identity_tuple(x: int, y: int) -> Tuple[int, int]:
            return x, y

        with self.dag:
            ident = identity_tuple(35, 36)

        dr = self.dag.create_dagrun(
            run_id=DagRunType.MANUAL.value,
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING,
        )

        ident.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        ti = dr.get_task_instances()[0]

        assert not ident.operator.multiple_outputs
        assert ti.xcom_pull() == [35, 36]
        assert ti.xcom_pull(key="return_value_0") is None
        assert ti.xcom_pull(key="return_value_1") is None

    def test_fails_bad_signature(self):
        """Tests that @task will fail if signature is not binding."""

        @task_decorator
        def add_number(num: int) -> int:
            return num + 2

        with pytest.raises(TypeError):
            add_number(2, 3)
        with pytest.raises(TypeError):
            add_number()
        add_number('test')

    def test_fail_method(self):
        """Tests that @task will fail if signature is not binding."""

        with pytest.raises(TypeError):

            class Test:
                num = 2

                @task_decorator
                def add_number(self, num: int) -> int:
                    return self.num + num

    def test_fail_multiple_outputs_key_type(self):
        @task_decorator(multiple_outputs=True)
        def add_number(num: int):
            return {2: num}

        with self.dag:
            ret = add_number(2)
        self.dag.create_dagrun(
            run_id=DagRunType.MANUAL,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
        )

        with pytest.raises(AirflowException):

            ret.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

    def test_fail_multiple_outputs_no_dict(self):
        @task_decorator(multiple_outputs=True)
        def add_number(num: int):
            return num

        with self.dag:
            ret = add_number(2)
        self.dag.create_dagrun(
            run_id=DagRunType.MANUAL,
            execution_date=DEFAULT_DATE,
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
        )

        with pytest.raises(AirflowException):

            ret.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

    def test_python_callable_arguments_are_templatized(self):
        """Test @task op_args are templatized"""
        recorded_calls = []

        # Create a named tuple and ensure it is still preserved
        # after the rendering is done
        Named = namedtuple('Named', ['var1', 'var2'])
        named_tuple = Named('{{ ds }}', 'unchanged')

        task = task_decorator(
            # a Mock instance cannot be used as a callable function or test fails with a
            # TypeError: Object of type Mock is not JSON serializable
            build_recording_function(recorded_calls),
            dag=self.dag,
        )
        ret = task(4, date(2019, 1, 1), "dag {{dag.dag_id}} ran on {{ds}}.", named_tuple)

        self.dag.create_dagrun(
            run_id=DagRunType.MANUAL,
            execution_date=DEFAULT_DATE,
            data_interval=(DEFAULT_DATE, DEFAULT_DATE),
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
        )
        ret.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        ds_templated = DEFAULT_DATE.date().isoformat()
        assert len(recorded_calls) == 1
        assert_calls_equal(
            recorded_calls[0],
            Call(
                4,
                date(2019, 1, 1),
                f"dag {self.dag.dag_id} ran on {ds_templated}.",
                Named(ds_templated, 'unchanged'),
            ),
        )

    def test_python_callable_keyword_arguments_are_templatized(self):
        """Test PythonOperator op_kwargs are templatized"""
        recorded_calls = []

        task = task_decorator(
            # a Mock instance cannot be used as a callable function or test fails with a
            # TypeError: Object of type Mock is not JSON serializable
            build_recording_function(recorded_calls),
            dag=self.dag,
        )
        ret = task(an_int=4, a_date=date(2019, 1, 1), a_templated_string="dag {{dag.dag_id}} ran on {{ds}}.")
        self.dag.create_dagrun(
            run_id=DagRunType.MANUAL,
            execution_date=DEFAULT_DATE,
            data_interval=(DEFAULT_DATE, DEFAULT_DATE),
            start_date=DEFAULT_DATE,
            state=State.RUNNING,
        )
        ret.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        assert len(recorded_calls) == 1
        assert_calls_equal(
            recorded_calls[0],
            Call(
                an_int=4,
                a_date=date(2019, 1, 1),
                a_templated_string=f"dag {self.dag.dag_id} ran on {DEFAULT_DATE.date().isoformat()}.",
            ),
        )

    def test_manual_task_id(self):
        """Test manually setting task_id"""

        @task_decorator(task_id='some_name')
        def do_run():
            return 4

        with self.dag:
            do_run()
            assert ['some_name'] == self.dag.task_ids

    def test_multiple_calls(self):
        """Test calling task multiple times in a DAG"""

        @task_decorator
        def do_run():
            return 4

        with self.dag:
            do_run()
            assert ['do_run'] == self.dag.task_ids
            do_run_1 = do_run()
            do_run_2 = do_run()
            assert ['do_run', 'do_run__1', 'do_run__2'] == self.dag.task_ids

        assert do_run_1.operator.task_id == 'do_run__1'
        assert do_run_2.operator.task_id == 'do_run__2'

    def test_multiple_calls_in_task_group(self):
        """Test calling task multiple times in a TaskGroup"""

        @task_decorator
        def do_run():
            return 4

        group_id = "KnightsOfNii"
        with self.dag:
            with TaskGroup(group_id=group_id):
                do_run()
                assert [f"{group_id}.do_run"] == self.dag.task_ids
                do_run()
                assert [f"{group_id}.do_run", f"{group_id}.do_run__1"] == self.dag.task_ids

        assert len(self.dag.task_ids) == 2

    def test_call_20(self):
        """Test calling decorated function 21 times in a DAG"""

        @task_decorator
        def __do_run():
            return 4

        with self.dag:
            __do_run()
            for _ in range(20):
                __do_run()

        assert self.dag.task_ids[-1] == '__do_run__20'

    def test_multiple_outputs(self):
        """Tests pushing multiple outputs as a dictionary"""

        @task_decorator(multiple_outputs=True)
        def return_dict(number: int):
            return {'number': number + 1, '43': 43}

        test_number = 10
        with self.dag:
            ret = return_dict(test_number)

        dr = self.dag.create_dagrun(
            run_id=DagRunType.MANUAL,
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING,
        )

        ret.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        ti = dr.get_task_instances()[0]
        assert ti.xcom_pull(key='number') == test_number + 1
        assert ti.xcom_pull(key='43') == 43
        assert ti.xcom_pull() == {'number': test_number + 1, '43': 43}

    def test_default_args(self):
        """Test that default_args are captured when calling the function correctly"""

        @task_decorator
        def do_run():
            return 4

        with self.dag:
            ret = do_run()
        assert ret.operator.owner == 'airflow'

        @task_decorator
        def test_apply_default_raise(unknown):
            return unknown

        with pytest.raises(TypeError):
            with self.dag:
                test_apply_default_raise()

        @task_decorator
        def test_apply_default(owner):
            return owner

        with self.dag:
            ret = test_apply_default()
        assert 'owner' in ret.operator.op_kwargs

    def test_xcom_arg(self):
        """Tests that returned key in XComArg is returned correctly"""

        @task_decorator
        def add_2(number: int):
            return number + 2

        @task_decorator
        def add_num(number: int, num2: int = 2):
            return number + num2

        test_number = 10

        with self.dag:
            bigger_number = add_2(test_number)
            ret = add_num(bigger_number, XComArg(bigger_number.operator))

        dr = self.dag.create_dagrun(
            run_id=DagRunType.MANUAL,
            start_date=timezone.utcnow(),
            execution_date=DEFAULT_DATE,
            state=State.RUNNING,
        )

        bigger_number.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        ret.operator.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)
        ti_add_num = [ti for ti in dr.get_task_instances() if ti.task_id == 'add_num'][0]
        assert ti_add_num.xcom_pull(key=ret.key) == (test_number + 2) * 2

    def test_dag_task(self):
        """Tests dag.task property to generate task"""

        @self.dag.task
        def add_2(number: int):
            return number + 2

        test_number = 10
        res = add_2(test_number)
        add_2(res)

        assert 'add_2' in self.dag.task_ids

    def test_dag_task_multiple_outputs(self):
        """Tests dag.task property to generate task with multiple outputs"""

        @self.dag.task(multiple_outputs=True)
        def add_2(number: int):
            return {'1': number + 2, '2': 42}

        test_number = 10
        add_2(test_number)
        add_2(test_number)

        assert 'add_2' in self.dag.task_ids

    def test_task_documentation(self):
        """Tests that task_decorator loads doc_md from function doc"""

        @task_decorator
        def add_2(number: int):
            """
            Adds 2 to number.
            """
            return number + 2

        test_number = 10
        with self.dag:
            ret = add_2(test_number)

        assert ret.operator.doc_md.strip(), "Adds 2 to number."


def test_mapped_decorator() -> None:
    @task_decorator
    def double(number: int):
        return number * 2

    with DAG('test_dag', start_date=DEFAULT_DATE):
        literal = [1, 2, 3]
        doubled_0 = double.map(number=literal)
        doubled_1 = double.map(number=literal)

    assert isinstance(doubled_0, XComArg)
    assert isinstance(doubled_0.operator, MappedOperator)
    assert doubled_0.operator.task_id == "double"
    assert doubled_0.operator.mapped_kwargs == {"op_args": [], "op_kwargs": {"number": literal}}

    assert doubled_1.operator.task_id == "double__1"


def test_mapped_decorator_invalid_args() -> None:
    @task_decorator
    def double(number: int):
        return number * 2

    with DAG('test_dag', start_date=DEFAULT_DATE):
        literal = [1, 2, 3]

        with pytest.raises(TypeError, match="arguments 'other', 'b'"):
            double.partial(other=1, b='a')
        with pytest.raises(TypeError, match="argument 'other'"):
            double.map(number=literal, other=1)


def test_partial_mapped_decorator() -> None:
    @task_decorator
    def product(number: int, multiple: int):
        return number * multiple

    literal = [1, 2, 3]

    with DAG('test_dag', start_date=DEFAULT_DATE) as dag:
        quadrupled = product.partial(multiple=3).map(number=literal)
        doubled = product.partial(multiple=2).map(number=literal)
        trippled = product.partial(multiple=3).map(number=literal)

        product.partial(multiple=2)  # No operator is actually created.

    assert dag.task_dict == {
        "product": quadrupled.operator,
        "product__1": doubled.operator,
        "product__2": trippled.operator,
    }

    assert isinstance(doubled, XComArg)
    assert isinstance(doubled.operator, DecoratedMappedOperator)
    assert doubled.operator.mapped_kwargs == {"op_args": [], "op_kwargs": {"number": literal}}
    assert doubled.operator.partial_op_kwargs == {"multiple": 2}

    assert isinstance(trippled.operator, DecoratedMappedOperator)  # For type-checking on partial_kwargs.
    assert trippled.operator.partial_op_kwargs == {"multiple": 3}

    assert doubled.operator is not trippled.operator


def test_mapped_decorator_unmap_merge_op_kwargs():
    with DAG("test-dag", start_date=datetime(2020, 1, 1)) as dag:

        @task_decorator
        def task1():
            ...

        @task_decorator
        def task2(arg1, arg2):
            ...

        task2.partial(arg1=1).map(arg2=task1())

    unmapped = dag.get_task("task2").unmap()
    assert set(unmapped.op_kwargs) == {"arg1", "arg2"}


def test_mapped_decorator_converts_partial_kwargs():
    with DAG("test-dag", start_date=datetime(2020, 1, 1)) as dag:

        @task_decorator
        def task1(arg):
            ...

        @task_decorator(retry_delay=30)
        def task2(arg1, arg2):
            ...

        task2.partial(arg1=1).map(arg2=task1.map(arg=[1, 2]))

    mapped_task2 = dag.get_task("task2")
    assert mapped_task2.partial_kwargs["retry_delay"] == timedelta(seconds=30)
    assert mapped_task2.unmap().retry_delay == timedelta(seconds=30)

    mapped_task1 = dag.get_task("task1")
    assert mapped_task2.partial_kwargs["retry_delay"] == timedelta(seconds=30)  # Operator default.
    mapped_task1.unmap().retry_delay == timedelta(seconds=300)  # Operator default.
