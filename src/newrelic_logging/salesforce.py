import base64
import csv
import json
import sys
from datetime import datetime, timedelta
import jwt
from cryptography.hazmat.primitives import serialization
import pytz
from requests import RequestException
import copy
import hashlib
from .cache import make_cache
from .env import Auth, AuthEnv
from .query import Query, substitute
from .telemetry import print_info, print_err

class LoginException(Exception):
    pass

class SalesforceApiException(Exception):
    err_code = 0
    def __init__(self, err_code: int, *args: object) -> None:
        self.err_code = err_code
        super().__init__(*args)
    pass

SALESFORCE_CREATED_DATE_QUERY = \
    "SELECT Id,EventType,CreatedDate,LogDate,Interval,LogFile,Sequence From EventLogFile Where CreatedDate>={" \
    "from_timestamp} AND CreatedDate<{to_timestamp} AND Interval='{log_interval_type}'"
SALESFORCE_LOG_DATE_QUERY = \
    "SELECT Id,EventType,CreatedDate,LogDate,Interval,LogFile,Sequence From EventLogFile Where LogDate>={" \
    "from_timestamp} AND LogDate<{to_timestamp} AND Interval='{log_interval_type}'"

CSV_SLICE_SIZE = 1000

def base64_url_encode(json_obj):
    json_str = json.dumps(json_obj)
    encoded_bytes = base64.urlsafe_b64encode(json_str.encode('utf-8'))
    encoded_str = str(encoded_bytes, 'utf-8')
    return encoded_str

class SalesForce:
    auth = None
    oauth_type = None
    token_url = ''
    query_template = None
    data_cache = None
    default_api_ver = ''

    def __init__(self, auth_env: AuthEnv, instance_name, config, event_type_fields_mapping, initial_delay, queries=[]):
        self.instance_name = instance_name
        self.default_api_ver = config.get('api_ver', '52.0')
        if 'auth' in config:
            self.auth_data = config['auth']
        else:
            self.auth_data = {'grant_type': auth_env.get_grant_type()}
            if self.auth_data['grant_type'] == 'password':
                # user/pass flow
                try:
                    self.auth_data["client_id"] = auth_env.get_client_id()
                    self.auth_data["client_secret"] = auth_env.get_client_secret()
                    self.auth_data["username"] = auth_env.get_username()
                    self.auth_data["password"] = auth_env.get_password()
                except:
                    print_err(f'Missing credentials for user/pass flow')
                    sys.exit(1)
            elif self.auth_data['grant_type'] == 'urn:ietf:params:oauth:grant-type:jwt-bearer':
                # jwt flow
                try:
                    self.auth_data["client_id"] = auth_env.get_client_id()
                    self.auth_data["private_key"] = auth_env.get_private_key()
                    self.auth_data["subject"] = auth_env.get_subject()
                    self.auth_data["audience"] = auth_env.get_audience()
                except:
                    print_err(f'Missing credentials for JWT flow')
                    sys.exit(1)
            else:
                print_err(f'Wrong or missing grant_type')
                sys.exit(1)

        if 'token_url' in config:
            self.token_url = config['token_url']
        else:
            self.token_url = auth_env.get_token_url()

        try:
            self.time_lag_minutes = config['time_lag_minutes']
            self.generation_interval = config['generation_interval']
            self.date_field = config['date_field']
        except KeyError as e:
            print_err(f'Please specify a "{e.args[0]}" parameter for sfdc instance "{instance_name}" in config.yml')
            sys.exit(1)

        self.last_to_timestamp = (datetime.utcnow() - timedelta(
            minutes=self.time_lag_minutes + initial_delay)).isoformat(timespec='milliseconds') + "Z"

        if len(queries) > 0:
            self.query_template = queries
        else:
            if self.date_field.lower() == "logdate":
                self.query_template = SALESFORCE_LOG_DATE_QUERY
            else:
                self.query_template = SALESFORCE_CREATED_DATE_QUERY

        self.data_cache = make_cache(config)
        self.event_type_fields_mapping = event_type_fields_mapping

    def clear_auth(self):
        if self.data_cache:
            try:
                self.data_cache.redis.delete("auth")
            except Exception as e:
                print_err(f"Failed deleting 'auth' key from Redis: {e}")
                exit(1)
        self.auth = None

    def store_auth(self, auth_resp):
        access_token = auth_resp['access_token']
        instance_url = auth_resp['instance_url']
        token_type = auth_resp['token_type']
        if self.data_cache:
            print_info("Storing credentials on Redis.")
            auth = {
                "access_token": access_token,
                "instance_url": instance_url,
                "token_type": token_type
            }
            try:
                self.data_cache.redis.hmset("auth", auth)
            except Exception as e:
                print_err(f"Failed setting 'auth' key: {e}")
                exit(1)
        self.auth = Auth(access_token, instance_url, token_type)

    def authenticate(self, oauth_type, session):
        self.oauth_type = oauth_type
        if self.data_cache:
            try:
                auth_exists = self.data_cache.redis.exists("auth")
            except Exception as e:
                print_err(f"Failed checking 'auth' key: {e}")
                exit(1)
            if auth_exists:
                print_info("Retrieving credentials from Redis.")
                #NOTE: hmget and hgetall both return byte arrays, not strings. We have to convert.
                # We could fix it by adding the argument "decode_responses=True" to Redis constructor,
                # but then we would have to change all places where we assume a byte array instead of a string,
                # and refactoring in a language without static types is a pain.
                try:
                    auth = self.data_cache.redis.hmget("auth", ["access_token", "instance_url", "token_type"])
                    auth = {
                        "access_token": auth[0].decode("utf-8"),
                        "instance_url": auth[1].decode("utf-8"),
                        "token_type": auth[2].decode("utf-8")
                    }
                    self.store_auth(auth)
                except Exception as e:
                    print_err(f"Failed getting 'auth' key: {e}")
                    exit(1)

                return True

        if oauth_type == 'password':
            if not self.authenticate_with_password(session):
                print_err(f"Error authenticating with {self.token_url}")
                return False
            print_info("Correctly authenticated with user/pass flow")
        else:
            if not self.authenticate_with_jwt(session):
                print_err(f"Error authenticating with {self.token_url}")
                return False
            print_info("Correctly authenticated with JWT flow")
        return True

    def authenticate_with_jwt(self, session):
        try:
            private_key_file = self.auth_data['private_key']
            client_id = self.auth_data['client_id']
            subject = self.auth_data['subject']
            audience = self.auth_data['audience']
        except KeyError as e:
            print_err(f'Please specify a "{e.args[0]}" parameter under "auth" section '
                  'of salesforce instance in config.yml')
            sys.exit(1)

        exp = int((datetime.utcnow() - timedelta(minutes=5)).timestamp())

        private_key = open(private_key_file, 'r').read()
        try:
            key = serialization.load_ssh_private_key(private_key.encode(), password=b'')
        except ValueError as e:
            print_err(f'Authentication failed for {self.instance_name}. error message: {str(e)}')
            return False

        jwt_claim_set = {"iss": client_id,
                         "sub": subject,
                         "aud": audience,
                         "exp": exp}

        signed_token = jwt.encode(
            jwt_claim_set,
            key,
            algorithm='RS256',
        )

        params = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_token,
            "format": "json"
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }

        try:
            print_info(f'retrieving salesforce token at {self.token_url}')
            resp = session.post(self.token_url, params=params,
                                headers=headers)
            if resp.status_code != 200:
                error_message = f'sfdc token request failed. http-status-code:{resp.status_code}, reason: {resp.text}'
                print_err(f'Authentication failed for {self.instance_name}. message: {error_message}', file=sys.stderr)
                return False

            self.store_auth(resp.json())
            return True
        except ConnectionError as e:
            print_err(f"SFDC auth failed for instance {self.instance_name}: {repr(e)}")
            raise LoginException(f'authentication failed for sfdc instance {self.instance_name}') from e
        except RequestException as e:
            print_err(f"SFDC auth failed for instance {self.instance_name}: {repr(e)}")
            raise LoginException(f'authentication failed for sfdc instance {self.instance_name}') from e

    def authenticate_with_password(self, session):
        client_id = self.auth_data['client_id']
        client_secret = self.auth_data['client_secret']
        username = self.auth_data['username']
        password = self.auth_data['password']

        params = {
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": client_secret,
            "username": username,
            "password": password
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        }

        try:
            print_info(f'retrieving salesforce token at {self.token_url}')
            resp = session.post(self.token_url, params=params,
                                headers=headers)
            if resp.status_code != 200:
                error_message = f'salesforce token request failed. status-code:{resp.status_code}, reason: {resp.reason}'
                print_err(error_message)
                return False

            self.store_auth(resp.json())
            return True
        except ConnectionError as e:
            print_err(f"SFDC auth failed for instance {self.instance_name}: {repr(e)}")
            raise LoginException(f'authentication failed for sfdc instance {self.instance_name}') from e
        except RequestException as e:
            print_err(f"SFDC auth failed for instance {self.instance_name}: {repr(e)}")
            raise LoginException(f'authentication failed for sfdc instance {self.instance_name}') from e

    def make_multiple_queries(self, query_objects) -> list[Query]:
        return [self.make_single_query(Query(obj)) for obj in query_objects]

    def make_single_query(self, query_obj: Query) -> Query:
        to_timestamp = (datetime.utcnow() - timedelta(minutes=self.time_lag_minutes)).isoformat(
            timespec='milliseconds') + "Z"
        from_timestamp = self.last_to_timestamp

        env = copy.deepcopy(query_obj.get_env().get('env', {}))
        args = {
            'to_timestamp': to_timestamp,
            'from_timestamp': from_timestamp,
            'log_interval_type': self.generation_interval
        }
        query = substitute(args, query_obj.get_query(), env)
        query = query.replace(' ', '+')

        query_obj.set_query(query)
        return query_obj

    def slide_time_range(self):
        self.last_to_timestamp = (datetime.utcnow() - timedelta(minutes=self.time_lag_minutes)).isoformat(
            timespec='milliseconds') + "Z"

    def execute_query(self, query: Query, session):
        api_ver = query.get_env().get("api_ver", self.default_api_ver)
        url = f'{self.auth.get_instance_url()}/services/data/v{api_ver}/query?q={query.get_query()}'

        try:
            headers = {
                'Authorization': f'Bearer {self.auth.get_access_token()}'
            }
            query_response = session.get(url, headers=headers)
            if query_response.status_code != 200:
                error_message = f'salesforce event log query failed. ' \
                                f'status-code:{query_response.status_code}, ' \
                                f'reason: {query_response.reason} ' \
                                f'response: {query_response.text} '

                print_err(f"SOQL query failed with code {query_response.status_code}: {error_message}")
                raise SalesforceApiException(query_response.status_code, f'error when trying to run SOQL query. message: {error_message}')
            return query_response.json()
        except RequestException as e:
            print_err(f"Error while trying SOQL query: {repr(e)}")
            raise SalesforceApiException(-1, f'error when trying to run SOQL query. cause: {e}') from e

    # NOTE: Is it possible that different SF orgs have overlapping IDs? If this is possible, we should use a different
    #       database for each org, or add a prefix to keys to avoid conflicts.

    def download_file(self, session, url):
        print_info(f"Downloading CSV file: {url}")

        headers = {
            'Authorization': f'Bearer {self.auth.get_access_token()}'
        }
        response = session.get(url, headers=headers)
        if response.status_code != 200:
            error_message = f'salesforce event log file download failed. ' \
                            f'status-code: {response.status_code}, ' \
                            f'reason: {response.reason} ' \
                            f'response: {response.text}'
            print_err(error_message)
            raise SalesforceApiException(response.status_code, error_message)
        return response

    def parse_csv(self, download_response, record_id, record_event_type, cached_messages):
        content = download_response.content.decode('utf-8')
        reader = csv.DictReader(content.splitlines())
        rows = []
        for row in reader:
            if self.data_cache and self.data_cache.record_or_skip_row(record_id, row, cached_messages):
                continue
            rows.append(row)
        return rows

    def fetch_logs(self, session):
        print_info(f"Query object = {self.query_template}")

        if type(self.query_template) is list:
            # "query_template" contains a list of objects, each one is a Query object
            queries = self.make_multiple_queries(copy.deepcopy(self.query_template))
            response = self.fetch_logs_from_multiple_req(session, queries)
            self.slide_time_range()
            return response
        else:
            # "query_template" contains a string with the SOQL to run.
            query = self.make_single_query(Query(self.query_template))
            response = self.fetch_logs_from_single_req(session, query)
            self.slide_time_range()
            return response

    def fetch_logs_from_multiple_req(self, session, queries: list[Query]):
        logs = []
        for query in queries:
            part_logs = self.fetch_logs_from_single_req(session, query)
            logs.extend(part_logs)
        return logs

    def fetch_logs_from_single_req(self, session, query: Query):
        print_info(f'Running query {query.get_query()}')
        response = self.execute_query(query, session)

        # Show query response
        #print("Response = ", response)

        records = response['records']
        if self.is_logfile_response(records):
            logs = []
            for record in records:
                if 'LogFile' in record:
                    log = self.build_log_from_logfile(True, session, record, query)
                    if log is not None:
                        logs.extend(log)
        else:
            logs = self.build_log_from_event(records, query)

        return logs

    def is_logfile_response(self, records):
        if len(records) > 0:
            return 'LogFile' in records[0]
        else:
            return True

    def build_log_from_event(self, records, query: Query):
        logs = []
        while True:
            part_rows = self.extract_row_slice(records)
            if len(part_rows) > 0:
                logs.append(self.pack_event_into_log(part_rows, query))
            else:
                break
        return logs

    def pack_event_into_log(self, rows, query: Query):
        log_entries = []
        for row in rows:
            if 'Id' in row:
                record_id = row['Id']
                if self.data_cache and self.data_cache.check_cached_id(record_id):
                    # Record cached, skip it
                    continue
            else:
                id_keys = query.get_env().get("id", [])
                compound_id = ""
                for key in id_keys:
                    if key not in row:
                        print_err(f"Error building compound id, key '{key}' not found")
                        raise Exception(f"Error building compound id, key '{key}' not found")
                    compound_id = compound_id + str(row.get(key, ""))
                if compound_id != "":
                    m = hashlib.sha3_256()
                    m.update(compound_id.encode('utf-8'))
                    row['Id'] = m.hexdigest()
                    record_id = row['Id']
                    if self.data_cache and self.data_cache.check_cached_id(record_id):
                        # Record cached, skip it
                        continue

            timestamp_attr = query.get_env().get("timestamp_attr", "CreatedDate")
            if timestamp_attr in row:
                created_date = row[timestamp_attr]
                timestamp = int(datetime.strptime(created_date, '%Y-%m-%dT%H:%M:%S.%f%z').timestamp() * 1000)
            else:
                created_date = ""
                timestamp = int(datetime.now().timestamp() * 1000)

            message = query.get_env().get("event_type", "SFEvent")
            if 'attributes' in row and type(row['attributes']) == dict:
                attributes = row.pop('attributes', [])
                if 'type' in attributes and type(attributes['type']) == str:
                    event_type_attr_name = query.get_env().get("event_type", attributes['type'])
                    message = event_type_attr_name
                    row['EVENT_TYPE'] = event_type_attr_name

            if created_date != "":
                message = message + " " + created_date

            timestamp_field_name = query.get_env().get("rename_timestamp", "timestamp")
            row[timestamp_field_name] = int(timestamp)

            log_entry = {
                'message': message,
                'attributes': row,
            }

            if timestamp_field_name == 'timestamp':
                log_entry[timestamp_field_name] = timestamp

            log_entries.append(log_entry)
        return {
            'log_entries': log_entries
        }

    def build_log_from_logfile(self, retry, session, record, query: Query):
        record_file_name = record['LogFile']
        record_id = str(record['Id'])
        interval = record['Interval']
        record_event_type = query.get_env().get("event_type", record['EventType'])

        # NOTE: only Hourly logs can be skipped, because Daily logs can change and the same record_id can contain different data.
        if interval == 'Hourly' and self.data_cache and \
            self.data_cache.can_skip_downloading_record(record_id):
            print_info(f"Record {record_id} already cached, skip downloading CSV")
            return None

        cached_messages = None if not self.data_cache else \
            self.data_cache.retrieve_cached_message_list(record_id)

        try:
            download_response = self.download_file(session, f'{self.auth.get_instance_url()}{record_file_name}')
            if download_response is None:
                return None
        except SalesforceApiException as e:
            if e.err_code == 401:
                if retry:
                    print_err("invalid token while downloading CSV file, retry auth and download...")
                    self.clear_auth()
                    if self.authenticate(self.oauth_type, session):
                        return self.build_log_from_logfile(False, session, record, query)
                    else:
                        return None
                else:
                    print_err(f'salesforce event log file "{record_file_name}" download failed: {e}')
                    return None
            else:
                print_err(f'salesforce event log file "{record_file_name}" download failed: {e}')
                return None
        except RequestException as e:
            print_err(f'salesforce event log file "{record_file_name}" download failed: {e}')
            return None

        csv_rows = self.parse_csv(download_response, record_id, record_event_type, cached_messages)

        print_info(f"CSV rows = {len(csv_rows)}")

        # Split CSV rows into smaller chunks to avoid hitting API payload limits
        logs = []
        row_offset = 0
        while True:
            part_rows = self.extract_row_slice(csv_rows)
            part_rows_len = len(part_rows)
            if part_rows_len > 0:
                logs.append(self.pack_csv_into_log(record, row_offset, part_rows, query))
                row_offset += part_rows_len
            else:
                break

        return logs

    def pack_csv_into_log(self, record, row_offset, csv_rows, query: Query):
        record_id = str(record['Id'])
        record_event_type = query.get_env().get("event_type", record['EventType'])

        log_entries = []
        for row_index, row in enumerate(csv_rows):
            message = {}
            if record_event_type in self.event_type_fields_mapping:
                for field in self.event_type_fields_mapping[record_event_type]:
                    message[field] = row[field]
            else:
                message = row

            if row.get('TIMESTAMP'):
                timestamp_obj = datetime.strptime(row.get('TIMESTAMP'), '%Y%m%d%H%M%S.%f')
                timestamp = pytz.utc.localize(timestamp_obj).replace(microsecond=0).timestamp()
            else:
                timestamp = datetime.utcnow().replace(microsecond=0).timestamp()

            message['LogFileId'] = record_id
            message.pop('TIMESTAMP', None)

            actual_event_type = message.pop('EVENT_TYPE', "SFEvent")
            new_event_type = query.get_env().get("event_type", actual_event_type)
            message['EVENT_TYPE'] = new_event_type

            timestamp_field_name = query.get_env().get("rename_timestamp", "timestamp")
            message[timestamp_field_name] = int(timestamp)

            log_entry = {
                'message': "LogFile " + record_id + " row " + str(row_index + row_offset),
                'attributes': message
            }

            if timestamp_field_name == 'timestamp':
                log_entry[timestamp_field_name] = int(timestamp)

            log_entries.append(log_entry)

        return {
            'log_type': record_event_type,
            'Id': record_id,
            'CreatedDate': record['CreatedDate'],
            'LogDate': record['LogDate'],
            'log_entries': log_entries
        }

    # Slice record into smaller chunks
    def extract_row_slice(self, rows):
        part_rows = []
        i = 0
        while len(rows) > 0:
            part_rows.append(rows.pop())
            i += 1
            if i >= CSV_SLICE_SIZE:
                break
        return part_rows
