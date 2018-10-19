# Copyright (c) 2018 Cisco and/or its affiliates.
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
#
import copy
from functools import partial

from ansible.module_utils.six import iteritems, viewitems

try:
    from ansible.module_utils.common import HTTPMethod, equal_objects, FtdConfigurationError, \
        FtdServerError, ResponseParams
    from ansible.module_utils.fdm_swagger_client import OperationField, ValidationError
except ImportError:
    from module_utils.common import HTTPMethod, equal_objects, FtdConfigurationError, \
        FtdServerError, ResponseParams
    from module_utils.fdm_swagger_client import OperationField, ValidationError

DEFAULT_PAGE_SIZE = 10
DEFAULT_OFFSET = 0

UNPROCESSABLE_ENTITY_STATUS = 422
INVALID_UUID_ERROR_MESSAGE = "Validation failed due to an invalid UUID"
DUPLICATE_NAME_ERROR_MESSAGE = "Validation failed due to a duplicate name"


class _OperationNamePrefix:
    ADD = 'add'
    EDIT = 'edit'
    GET = 'get'
    DELETE = 'delete'


class ParamName:
    QUERY_PARAMS = 'query_params'
    PATH_PARAMS = 'path_params'
    DATA = 'data'
    FILTERS = 'filter'


class CheckModeException(Exception):
    pass


class FtdInvalidOperationNameError(Exception):
    def __init__(self, operation_name):
        super(FtdInvalidOperationNameError, self).__init__(operation_name)
        self.operation_name = operation_name


class BaseConfigurationResource(object):
    def __init__(self, conn, check_mode=False):
        self._conn = conn
        self.config_changed = False
        self._operation_spec_cache = {}
        self._models_operations_specs_cache = {}
        self._check_mode = check_mode

    def crud_operation(self, op_name, params):
        op_spec = self.get_operation_spec(op_name)
        if op_spec is None:
            raise FtdInvalidOperationNameError(op_name)

        if self.is_add_operation(op_name):
            resp = self.add_object(op_name, params)
        elif self.is_edit_operation(op_name):
            resp = self.edit_object(op_name, params)
        elif self.is_delete_operation(op_name):
            resp = self.delete_object(op_name, params)
        elif self.is_find_by_filter_operation(op_name, params):
            resp = self.get_objects_by_filter(op_name, params)
        else:
            resp = self.send_general_request(op_name, params)
        return resp

    def get_operation_spec(self, operation_name):
        if operation_name not in self._operation_spec_cache:
            self._operation_spec_cache[operation_name] = self._conn.get_operation_spec(operation_name)
        return self._operation_spec_cache[operation_name]

    def get_operation_specs_by_model_name(self, model_name):
        if model_name not in self._models_operations_specs_cache:
            model_op_specs = self._conn.get_operation_specs_by_model_name(model_name)
            self._models_operations_specs_cache[model_name] = model_op_specs
            for op_name, op_spec in iteritems(model_op_specs):
                self._operation_spec_cache.setdefault(op_name, op_spec)
        return self._models_operations_specs_cache[model_name]

    def get_objects_by_filter(self, operation_name, params, get_one_item=False):
        def transform_filters_to_query_param(filter_params):
            return ';'.join(['%s:%s' % (key, val) for key, val in iteritems(filter_params)])

        def match_filters(filter_params, obj):
            return viewitems(filter_params) <= viewitems(obj)

        _, query_params, path_params = _get_user_params(params)
        # copy required params to avoid mutation of passed `params` dict
        get_list_params = {ParamName.QUERY_PARAMS: dict(query_params), ParamName.PATH_PARAMS: dict(path_params)}

        filters = params.get(ParamName.FILTERS) or {}
        if filters:
            get_list_params[ParamName.QUERY_PARAMS][ParamName.FILTERS] = transform_filters_to_query_param(filters)

        item_generator = iterate_over_pageable_resource(
            partial(self.send_general_request, operation_name=operation_name), get_list_params
        )
        if get_one_item:
            return next((i for i in item_generator if match_filters(filters, i)), None)
        else:
            return [i for i in item_generator if match_filters(filters, i)]

    def add_object(self, operation_name, params):
        def is_duplicate_name_error(err):
            return err.code == UNPROCESSABLE_ENTITY_STATUS and DUPLICATE_NAME_ERROR_MESSAGE in str(err)

        try:
            return self.send_general_request(operation_name, params)
        except FtdServerError as e:
            if is_duplicate_name_error(e):
                return self._check_if_the_same_object(operation_name, params, e)
            else:
                raise e

    def _check_if_the_same_object(self, operation_name, params, e):
        model_name = self.get_operation_spec(operation_name)[OperationField.MODEL_NAME]
        get_list_operation = self._find_get_list_operation(model_name)
        if get_list_operation:
            data = params[ParamName.DATA]
            if not params.get(ParamName.FILTERS):
                params[ParamName.FILTERS] = {'name': data['name']}

            existing_obj = self.get_objects_by_filter(get_list_operation, params, True)

            if existing_obj is not None:
                if equal_objects(existing_obj, data):
                    return existing_obj
                else:
                    raise FtdConfigurationError(
                        'Cannot add new object. '
                        'An object with the same name but different parameters already exists.',
                        existing_obj)

        raise e

    def _find_get_list_operation(self, model_name):
        operations = self.get_operation_specs_by_model_name(model_name) or {}
        return next((op for op in operations.keys() if self.is_get_list_operation(op)), None)

    def _find_get_operation(self, model_name):
        operations = self.get_operation_specs_by_model_name(model_name) or {}
        return next((op for op in operations.keys() if self.is_get_operation(op)), None)

    def delete_object(self, operation_name, params):
        def is_invalid_uuid_error(err):
            return err.code == UNPROCESSABLE_ENTITY_STATUS and INVALID_UUID_ERROR_MESSAGE in str(err)

        try:
            return self.send_general_request(operation_name, params)
        except FtdServerError as e:
            if is_invalid_uuid_error(e):
                return {'status': 'Referenced object does not exist'}
            else:
                raise e

    def edit_object(self, operation_name, params):
        data, _, path_params = _get_user_params(params)

        model_name = self.get_operation_spec(operation_name)[OperationField.MODEL_NAME]
        get_operation = self._find_get_operation(model_name)

        if get_operation:
            existing_object = self.send_general_request(get_operation, {ParamName.PATH_PARAMS: path_params})
            if not existing_object:
                raise FtdConfigurationError('Referenced object does not exist')
            elif equal_objects(existing_object, data):
                return existing_object

        return self.send_general_request(operation_name, params)

    def send_general_request(self, operation_name, params):
        def stop_if_check_mode():
            if self._check_mode:
                raise CheckModeException()

        self.validate_params(operation_name, params)
        stop_if_check_mode()

        data, query_params, path_params = _get_user_params(params)
        op_spec = self.get_operation_spec(operation_name)
        url, method = op_spec[OperationField.URL], op_spec[OperationField.METHOD]

        return self._send_request(url, method, data, path_params, query_params)

    def _send_request(self, url_path, http_method, body_params=None, path_params=None, query_params=None):
        def raise_for_failure(resp):
            if not resp[ResponseParams.SUCCESS]:
                raise FtdServerError(resp[ResponseParams.RESPONSE], resp[ResponseParams.STATUS_CODE])

        response = self._conn.send_request(url_path=url_path, http_method=http_method, body_params=body_params,
                                           path_params=path_params, query_params=query_params)
        raise_for_failure(response)
        if http_method != HTTPMethod.GET:
            self.config_changed = True
        return response[ResponseParams.RESPONSE]

    def is_add_operation(self, operation_name):
        operation_spec = self.get_operation_spec(operation_name)
        # Some endpoints have non-CRUD operations, so checking operation name is required in addition to the HTTP method
        return operation_name.startswith(_OperationNamePrefix.ADD) and is_post_request(operation_spec)

    def is_edit_operation(self, operation_name):
        operation_spec = self.get_operation_spec(operation_name)
        # Some endpoints have non-CRUD operations, so checking operation name is required in addition to the HTTP method
        return operation_name.startswith(_OperationNamePrefix.EDIT) and is_put_request(operation_spec)

    def is_delete_operation(self, operation_name):
        operation_spec = self.get_operation_spec(operation_name)
        # Some endpoints have non-CRUD operations, so checking operation name is required in addition to the HTTP method
        return operation_name.startswith(_OperationNamePrefix.DELETE) and operation_spec[
            OperationField.METHOD] == HTTPMethod.DELETE

    def is_get_list_operation(self, operation_name):
        op_spec = self.get_operation_spec(operation_name)
        return op_spec[OperationField.METHOD] == HTTPMethod.GET and op_spec[OperationField.RETURN_MULTIPLE_ITEMS]

    def is_get_operation(self, operation_name):
        op_spec = self.get_operation_spec(operation_name)
        return op_spec[OperationField.METHOD] == HTTPMethod.GET and not op_spec[OperationField.RETURN_MULTIPLE_ITEMS]

    def is_find_by_filter_operation(self, operation_name, params):
        """
        Checks whether the called operation is 'find by filter'. This operation fetches all objects and finds
        the matching ones by the given filter. As filtering is done on the client side, this operation should be used
        only when selected filters are not implemented on the server side.

        :param operation_name: name of the operation being called by the user
        :type operation_name: str
        :param operation_spec: specification of the operation being called by the user
        :type operation_spec: dict
        :param params: params - params should contain 'filter'
        :return: True if called operation is find by filter, otherwise False
        :rtype: bool
        """
        is_get_list = self.is_get_list_operation(operation_name)
        return is_get_list and ParamName.FILTERS in params and params[ParamName.FILTERS]

    def validate_params(self, operation_name, params):
        report = {}
        op_spec = self.get_operation_spec(operation_name)
        data, query_params, path_params = _get_user_params(params)

        def validate(validation_method, field_name, user_params):
            key = 'Invalid %s provided' % field_name
            try:
                is_valid, validation_report = validation_method(operation_name, user_params)
                if not is_valid:
                    report[key] = validation_report
            except Exception as e:
                report[key] = str(e)
            return report

        validate(self._conn.validate_query_params, ParamName.QUERY_PARAMS, query_params)
        validate(self._conn.validate_path_params, ParamName.PATH_PARAMS, path_params)
        if is_post_request(op_spec) or is_put_request(op_spec):
            validate(self._conn.validate_data, ParamName.DATA, data)

        if report:
            raise ValidationError(report)


def _set_default(params, field_name, value):
    if field_name not in params or params[field_name] is None:
        params[field_name] = value


def is_post_request(operation_spec):
    return operation_spec[OperationField.METHOD] == HTTPMethod.POST


def is_put_request(operation_spec):
    return operation_spec[OperationField.METHOD] == HTTPMethod.PUT


def _get_user_params(params):
    return params.get(ParamName.DATA) or {}, params.get(ParamName.QUERY_PARAMS) or {}, params.get(
        ParamName.PATH_PARAMS) or {}


def iterate_over_pageable_resource(resource_func, params):
    """
    A generator function that iterates over a resource that supports pagination and lazily returns present items
    one by one.

    :param resource_func: function that receives `params` argument and returns a page of objects
    :type resource_func: callable
    :param params: initial dictionary of parameters that will be passed to the resource_func.
                   Should contain `query_params` inside.
    :type params: dict
    :return: an iterator containing returned items
    :rtype: iterator of dict
    """
    # creating a copy not to mutate passed dict
    params = copy.deepcopy(params)
    params[ParamName.QUERY_PARAMS].setdefault('limit', DEFAULT_PAGE_SIZE)
    params[ParamName.QUERY_PARAMS].setdefault('offset', DEFAULT_OFFSET)

    result = resource_func(params=params)
    while result['items']:
        for item in result['items']:
            yield item
        # creating a copy not to mutate existing dict
        params = copy.deepcopy(params)
        query_params = params[ParamName.QUERY_PARAMS]
        query_params['offset'] = int(query_params['offset']) + int(query_params['limit'])
        result = resource_func(params=params)
