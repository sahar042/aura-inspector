# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import requests
import re, json
import traceback
from colored_logger import logger
from urllib.parse import urlparse, quote
from requests.packages.urllib3.exceptions import InsecureRequestWarning
import os
from http.cookies import SimpleCookie

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.16; rv:85.0) Gecko/210100101 Firefox/85.0'
AURA_ENDPOINTS = ['/s/sfsites/aura','/s/aura','/aura','/sfsites/aura']

class AuraActionHelper:
	def build_action(act_id, descriptor, params={}):
		return {
			'id':act_id,
			'descriptor':descriptor,
			'callingDescriptor':'UNKNOWN',
			'params':params
		}

	def build_context(fwuid, app, loaded):
		return json.dumps({
			"mode":"PROD",
			"fwuid":fwuid,
			"app":app,
			"loaded":loaded,
			"dn":[],
			"globals":{},
			"uad":False
		})

	def get_dummy_action():
		return AuraActionHelper.build_action(
			'242;a',
			'serviceComponent://ui.force.components.controllers.relatedList.RelatedListContainerDataProviderController/ACTION$getRecords',
			{"recordId":"Foobar"}
		)

	def get_dummy_context():
		return AuraActionHelper.build_context(
			"INVALID",
			"siteforce:loginApp2",
			{"APPLICATION@markup://siteforce:loginApp2":"siteforce:loginApp2"}
		)

class AuraActionResponse:
	def __init__(self, json_action):
		self.json_action = json_action
		self.id = None
		self.state = None
		self.return_value = None
		self.error_message = None
		self.parse_action_response()

	def parse_action_response(self):
		self.state = self.json_action['state']
		self.id = self.json_action['id']
		if self.is_success():
			self.return_value = self.json_action['returnValue']
		if self.is_error():
			error = self.json_action["error"][0]
			if 'event' in error:
				error_values = self.json_action["error"][0]["event"]["attributes"]["values"]
				if 'error' in error_values:
					self.error_message = error_values['error']['message']
				elif 'message' in error_values:
					self.error_message = error_values['message']
			elif 'message' in error:
				self.error_message = self.json_action["error"][0]["message"]

	def is_success(self):
		return self.state == 'SUCCESS'

	def is_error(self):
		return self.state == 'ERROR'

class AuraResponse:

	def __init__(self, response):
		self.response = response
		self.json_response = None
		self.actions_responses = []
		self.parse_response()

	def parse_response(self):
		if self.is_valid():
			self.json_response = self.response.json()
			for action in self.json_response.get('actions',[]):
				self.actions_responses.append(AuraActionResponse(action))
		else:
			logger.verbose(f"Invalid JSON response: {self.response.text}")

	def is_valid(self):
		try:
			self.response.json()
			return True
		except:
			False

class AuraResponses:

    def __init__(self, aura_responses):
        self.aura_responses = aura_responses
        self.actions_responses = []
        self.aggregate_action_responses()

    def aggregate_action_responses(self):
        #Make one list of action responses to aggregate bulked requests
        for aura_response in self.aura_responses:
            self.actions_responses += aura_response.actions_responses

class AuraHelper:

	def __init__(self, url, cookies, proxy, insecure, app, aura, context, token):

		self.url = url.rstrip('/')
		self.aura_token = 'undefined' if not token else token
		self.headers = {'User-Agent': USER_AGENT, 'Accept':'application/json'}
		self.session = requests.session()

		# If SID is not supplied, test guest access
		if cookies is None:
			logger.error('Cookies not supplied. This will only perform unauthenticated checks')
		else:
			parsed_cookies = SimpleCookie(cookies)
			for key, value in parsed_cookies.items():
				self.session.cookies.set(key, value)
			if self.session.cookies.get("sid") == None:
				logger.error("Cookies supplied but session cookie - SID not provided. This will only perform unauthenticated checks")

		self.objects = {}
		self.fwuid = None
		self.app = None
		self.csp_trusted = []
		self.gql_enabled = False
		self.rest_api_url = None
		self.rest_api_accessible = False
		self.session.verify = False if insecure else True
		self.session.proxies.update({} if not proxy else {'http':proxy, 'https':proxy})
   
		# Find the aura endpoint
		self.aura_endpoint = self.get_aura_endpoint() if not aura else aura
		logger.info(f'Using aura endpoint: {self.url}{self.aura_endpoint}')
		# Retrieve app information
		self.app = self.get_app() if not app else f"{self.url}/{app.lstrip('/')}"
		logger.info(f'Using app: {self.app}')
		# Retrieve the context including fwuid
		self.context = self.get_context() if not context else context
		logger.debug(f'Using context: {self.context}')
		# Finally get aura token
		self.aura_token = self.get_aura_token() if not token else token
		logger.debug(f'Using token: {self.aura_token}')

	def build_post_body(self, actions=[], dummy=False):
		message = {
			'message': json.dumps({'actions':[AuraActionHelper.get_dummy_action()]}) if dummy else json.dumps({'actions':actions}),
			'aura.context': AuraActionHelper.get_dummy_context() if dummy else self.context,
			'aura.pageURI': 'unknown',
			'aura.token': self.aura_token
		}
		return message

	def send_aura_bulk(self, actions=[], chunk_size=100, dummy=False):
		chunk_size = min(chunk_size,100) #Max is 100
		actions = [actions] if not isinstance(actions, list) else actions #Make it work with both lists and single actions
		actions_chunks = [actions[i:i+chunk_size] for i in range(0, len(actions), chunk_size)] #Split in chunks of 100
		aura_responses = []
		for i in range(len(actions_chunks)):
			chunk = actions_chunks[i]
			post_body = self.build_post_body(chunk)
			if len(chunk) > 1:
				logger.verbose(f"Sending bulk aura actions from {i*chunk_size} to {i*chunk_size+len(chunk)}")
			try:
				response = self.session.post(url=f"{self.url}{self.aura_endpoint}", headers=self.headers, data=post_body, timeout=90)
				aura_response = AuraResponse(response)
				aura_responses.append(aura_response)
			except requests.exceptions.SSLError as e:
				logger.error("Error when sending aura request, try using parameter -k to ignore invalid certificates")
				logger.debug(traceback.format_exc())
			except requests.exceptions.ReadTimeout as e:
				if chunk_size > 1:
					logger.error("Timeout when sending aura request, re-attempting to send the chunk slowly and without bulking...")
					aura_responses += self.send_aura_bulk(chunk, chunk_size=1).aura_responses
		return AuraResponses(aura_responses)

	def get_aura_endpoint(self):
		post_body = self.build_post_body(dummy=True)
		for endpoint in AURA_ENDPOINTS:
			try:
				post_request = self.session.post(f"{self.url}{endpoint}", allow_redirects=False, headers=self.headers, data=post_body)
				if 'markup://' in post_request.text:
					return endpoint
				elif post_request.status_code == 301 and post_request.headers.get('Location'):
					redir_url = post_request.headers.get('Location')
					post_request = self.session.post(redir_url, allow_redirects=False, headers=self.headers, data=post_body)
					if 'markup://' in post_request.text:
						return urlparse(redir_url).path
			except requests.exceptions.SSLError:
				logger.error("Error when trying to retrieve aura endpoint, try using parameter -k to ignore invalid certificates")
			except requests.exceptions.ConnectionError:
				logger.error("Cannot reach the target URL, aborting...")
				logger.debug(traceback.format_exc())
				exit()
			except:
				logger.error("Error when trying to retrieve aura endpoint")
				logger.debug(traceback.format_exc())
				pass
		#If we get out of the loop we did not find it
		logger.critical('Could not identify aura endpoint.')
		exit()

	def get_context(self):
		response_body = self.session.get(self.app, allow_redirects=True, headers=self.headers)
		aura_encoded = re.search(r'\/s\/sfsites\/l\/([^\/]+fwuid[^\/]+)', response_body.text)
		context = AuraActionHelper.get_dummy_context()
		if aura_encoded is None:
			if ("window.location.href ='%s" % self.url) in response_body.text:
				location_url = re.search(r'window.location.href =\'([^\']+)', response_body.text)
				url = location_url.group(1)
				try:
					response_body = self.session.get(url, allow_redirects=True, headers=self.headers)
				except Exception as e:
					logger.error("Failed to access the redirect url")
					raise
		fwuid = re.search(r'"fwuid":"([^"]+)', response_body.text)
		markup = re.search(r'"(APPLICATION@markup[^"]+)":"([^"]+)"', response_body.text)
		app = re.search(r'"app":"([^"]+)', response_body.text)
		if fwuid is None:
			post_body = self.build_post_body(dummy=True)
			retry_resp = self.session.post(f'{self.url}{self.aura_endpoint}', data=post_body, allow_redirects=True, headers=self.headers)
			resp_data = retry_resp.text
			fwuid_pattern = "Expected:(.*?) Actual"
			fwuid = re.search(fwuid_pattern, resp_data)

			if 'markup://aura:invalidSession' in resp_data:
				logger.critical('Invalid session when trying to get context, guest access might be disabled, aborting')
				exit()
			elif fwuid is None:
				try:
					json_resp_data = json.loads(resp_data)
				except json.JSONDecodeError:
					logger.critical('Could not parse aura response (non-JSON response received). The target may be unreachable, misconfigured, or guest access may be disabled.')
					logger.debug(f'Response body: {resp_data[:500]}')
					exit()
				if 'context' in json_resp_data:
					fwuid = json_resp_data['context']['fwuid']
				else:
					logger.critical('No context found in response, aborting')
					logger.debug(json_resp_data)
					exit()
			else:
				fwuid = fwuid.group(1).strip()
			app_data = 'siteforce:loginApp2'
			context = AuraActionHelper.build_context(fwuid,app_data,{f"APPLICATION@markup://{app_data}":app_data})
		else:
			context = AuraActionHelper.build_context(fwuid.group(1),app.group(1),{f"{markup.group(1)}":f"{markup.group(2)}"})
   
		return context


	def get_aura_token(self):
		logger.verbose('Retrieving aura token')
		response = self.session.get(f"{self.app}", allow_redirects=True, headers=self.headers)

		aura_token_pattern = r'eyJub[^";]+'
		aura_token = 'null'
		if aura_token_search := re.search(aura_token_pattern, response.text):
			aura_token = aura_token_search.group(0)
			logger.verbose(f'Found aura token in page: {aura_token}')
		elif 'set-cookie' in response.headers:
			if aura_token_search := re.search(aura_token_pattern, response.headers['set-cookie']):
				aura_token = aura_token_search.group(0)
				logger.verbose(f'Found aura token in cookie: {aura_token}')
		else:
			logger.error(f'Aura token not found (probably because SID cookie was not supplied), using null token')
   
		return aura_token


	def get_app(self):
		logger.verbose('Retrieving app')
		for endpoint in AURA_ENDPOINTS:
			if endpoint in self.aura_endpoint:
				return f'{self.url}{self.aura_endpoint.replace(endpoint,"")}/s'
		#If we got out of the loop we did not find it
		logger.error('App not found, using default app /s')
		return self.url + '/s'


	def get_objects(self):
		logger.verbose('Attempting to retrieve all objects and CSP trusted sites')
		action = AuraActionHelper.build_action("1;a","aura://HostConfigController/ACTION$getConfigData")
		objects = []
		try:
			action_response = self.send_aura_bulk(action).actions_responses[0]
			self.csp_trusted = action_response.return_value['cspTrustedSites']
			objects = list(action_response.return_value['apiNamesToKeyPrefixes'].keys())
			logger.info(f'Found {len(objects)} objects')
		except:
			logger.error("Error while retrieving objects and CSP trusted sites")
			logger.debug(traceback.format_exc())

		return objects

	def get_records(self, objects, page_size=100, fetch_all=False):

		results = {}
		actions = []
		for object_name in objects:
			params = {
				"entityNameOrId":object_name,
				"layoutType":"FULL",
				"pageSize":page_size,
				"currentPage":1,
				"useTimeout":False,
				"getCount":True,
				"enableRowActions":False
			}
			action = AuraActionHelper.build_action(
				object_name,
				"serviceComponent://ui.force.components.controllers.lists.selectableListDataProvider.SelectableListDataProviderController/ACTION$getItems",
				params
			)

			actions.append(action)

		logger.info(f"Attempting to retrieve information for {len(objects)} objects")
		actions_responses = self.send_aura_bulk(actions).actions_responses
		for action_response in actions_responses:
			object_name = action_response.id
			if action_response.is_success():
				total_count = action_response.return_value.get('totalCount') or 0
				records = action_response.return_value.get('result') or action_response.return_value.get('records') or []
				if not records:
					for key in action_response.return_value:
						val = action_response.return_value[key]
						if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
							records = val
							break
				results[object_name] = {'records': records, 'total_count': total_count}
				if records:
					logger.verbose(f'Retrieved {len(records)} records for {object_name}')
			elif action_response.is_error():
				logger.debug(f'Could not retrieve records for {object_name}: {action_response.error_message}')

		if fetch_all:
			for object_name in list(results.keys()):
				total_count = results[object_name]['total_count']
				fetched = len(results[object_name]['records'])
				if fetched > 0 and fetched < total_count:
					current_page = 2
					while fetched < total_count:
						logger.verbose(f'Fetching page {current_page} for {object_name} ({fetched}/{total_count} records)')
						action = AuraActionHelper.build_action(
							object_name,
							"serviceComponent://ui.force.components.controllers.lists.selectableListDataProvider.SelectableListDataProviderController/ACTION$getItems",
							{
								"entityNameOrId":object_name,
								"layoutType":"FULL",
								"pageSize":page_size,
								"currentPage":current_page,
								"useTimeout":False,
								"getCount":False,
								"enableRowActions":False
							}
						)
						try:
							page_response = self.send_aura_bulk(action).actions_responses[0]
							if page_response.is_success():
								page_records = page_response.return_value.get('result') or page_response.return_value.get('records') or []
								if not page_records:
									for key in page_response.return_value:
										val = page_response.return_value[key]
										if isinstance(val, list) and len(val) > 0 and isinstance(val[0], dict):
											page_records = val
											break
								if not page_records:
									break
								results[object_name]['records'].extend(page_records)
								fetched += len(page_records)
								current_page += 1
							else:
								logger.debug(f'Stopped paginating {object_name} at page {current_page}')
								break
						except Exception:
							logger.error(f'Error while paginating records for {object_name}')
							logger.debug(traceback.format_exc())
							break
					logger.verbose(f'Retrieved {len(results[object_name]["records"])} total records for {object_name}')

		logger.info(f'Retrieved information for {len(results)} objects')
		return results

	def get_records_ui_list(self, objects):

		results = set()
		objects_with_views = {}
		actions = []
		for i in range(len(objects)):

			object_name = objects[i]

			action = AuraActionHelper.build_action(
				object_name,
				"serviceComponent://ui.force.components.controllers.lists.listViewPickerDataProvider.ListViewPickerDataProviderController/ACTION$getInitialListViews",
				{
					"scope":object_name,
					"maxMruResults":10,
					"maxAllResults":20
				}
			)
			actions.append(action)

		logger.verbose(f"Attempting to retrieve UI lists for {len(objects)} objects")
		actions_responses = self.send_aura_bulk(actions).actions_responses
		for action_response in actions_responses:

			object_name = action_response.id
			try:
				if action_response.is_success() and len(action_response.return_value['listViews']) > 0:
					objects_with_views[object_name] = action_response
					# results.append(f'{self.app}/recordlist/{object_name}/Default')
				elif action_response.is_error():
					logger.debug(f'Error while retrieving UI lists: {action_response.error_message}')
			except:
				logger.error(f'Unhandled error while retrieving UI record lists for object {object_name}')
				logger.debug(traceback.format_exc())

		if len(objects_with_views) > 0:
			logger.info("Checking accessible views for each object")
			
			actions = []

			# Build action list
			for obj in objects_with_views:
				
				try:
					for filter in objects_with_views[obj].return_value['listViews']:
						action = AuraActionHelper.build_action(
							f'{obj};{filter["name"]}',
							"serviceComponent://ui.force.components.controllers.lists.listViewDataManager.ListViewDataManagerController/ACTION$getItems",
							{
								"filterName":filter['name'],
								"entityName":obj,
								"pageSize":50,
								"layoutType":"LIST",
								"getCount":True,
								"enableRowActions":False,
								"offset":0
							}
						)

						actions.append(action)
			
				except:
					logger.error(f'Unhandled error while retrieving UI record list for object {object_name}')

			actions_responses = self.send_aura_bulk(actions).actions_responses

			for action_response in actions_responses:
				try:
					object_name,filter_name = action_response.id.split(";")
					if action_response.is_success() and len(action_response.return_value['recordIdActionsList']) > 0:
						logger.verbose(f'Identified accessible record list for {object_name} for filter {filter_name}')
						results.add(f'{self.app}/recordlist/{object_name}/Default')
				except:
					logger.debug(f'Error while retrieveing parsing UI list response')

		else:
			logger.info(f'No UI record lists for the targeted objects')

		if len(results) > 0:
			logger.warning(f'Found {len(results)} UI record lists for the targeted objects, please check these URLs manually as they could display sensitive records')
		
		return list(results)

	def get_object_home_urls(self):

		logger.verbose('Attempting to retrieve object home URLs')
		action = AuraActionHelper.build_action(
			"17;a",
			"serviceComponent://ui.communities.components.aura.components.communitySetup.cmc.CMCAppController/ACTION$getAppBootstrapData",
		)

		results = []
		try:
			action_response = self.send_aura_bulk(action).actions_responses[0]
			if action_response.is_success():
				results = action_response.json_action['components'][0]['model']['apiNameToObjectHomeUrls']
				logger.warning(f'Found {len(results)} object home URLs, please check these URLs manually as they could contain sensitive panels')
			elif action_response.is_error():
				logger.verbose(f'Could not retrieve object home URLs: {action_response.error_message}')
		except:
			logger.error('Error while retrieving object home URLs')
			logger.debug(traceback.format_exc())

		return results

	def check_self_registration_enabled(self):

		logger.verbose('Checking if self-registration is enabled')
		actions = [
	  		AuraActionHelper.build_action("1", "apex://applauncher.LoginFormController/ACTION$getIsSelfRegistrationEnabled"),
			AuraActionHelper.build_action("2", "apex://applauncher.LoginFormController/ACTION$getSelfRegistrationUrl")
		]

		try:
			actions_responses = self.send_aura_bulk(actions).actions_responses
			is_enabled_response = actions_responses[0]
			url_response = actions_responses[1]
			if is_enabled_response.is_success() and is_enabled_response.return_value:
				selfreg_url = url_response.return_value
				logger.warning(f'Self-registration is enabled and URL is {selfreg_url}')
				return selfreg_url
			else:
				logger.info(f'Self-registration is not enabled')
		except:
			logger.error('Error while checking for self-registration, if you are using a SID cookie it is usually normal behavior')
			logger.debug(traceback.format_exc())

		return None

	def check_graphql_enabled(self):

		logger.verbose('Checking if GraphQL queries can be used')
		action = AuraActionHelper.build_action(
			"GraphQL",
			"aura://RecordUiController/ACTION$executeGraphQL",
			{
				"queryInput":
				{
					"operationName":"getUsersCount",
					"query":"query getUsersCount{uiapi{query{User{totalCount}}}}",
					"variables":{}
				}
			}
		)

		try:
			action_response = self.send_aura_bulk(action).actions_responses[0]
			if action_response.is_success():
				return_value = action_response.return_value
				if 'errors' in return_value and len(return_value['errors']) > 0:
					logger.debug(f"GraphQL is enabled, but it does not seem like the user can use it, error message: {return_value['errors']['message']}")
				else:
					logger.verbose("GraphQL is enabled, will try to prioritize it's use")
					self.gql_enabled = True
			elif action_response.is_error():
				try:
					logger.verbose(f"GraphQL is not available: {action_response.error_message}")
				except:
					logger.verbose('GraphQL is not available')
			else:
				raise Exception(f'Unknown error when checking if GraphQL is enabled')
		except Exception as e:
			logger.error('Error while checking if GraphQL is enabled')
			logger.debug(traceback.format_exc())

	def get_graphql_fields_for_objects(self, objects):
		logger.verbose("Retrieving field names for objects using GraphQL")
		banned_fields = ["CloneSourceId"] #Not handled properly by the tool (yet?)
		banned_types = ["ADDRESS","ANYTYPE","COMPLEXVALUE"] #Not handled properly by the tool (yet?)
		leaf_types = ["ID"] # Scalar types that don't support {value} subselection
		object_fields_map = {}

		# GraphQL apiNames is limited to 100 entries
		for i in range(0, len(objects), 100):
			batch = objects[i:i+100]

			#Formatting as follow objectInfos(apiNames:User,Account) etc...
			formatted_object_names = json.dumps(batch,separators=(',', ':'))
			action = AuraActionHelper.build_action(
				'1;fields',
				'aura://RecordUiController/ACTION$executeGraphQL',
				{
					'queryInput':{
						'operationName':'getFields',
						'query':'query getFields{uiapi{objectInfos(apiNames:%s){ApiName,fields{ApiName,dataType}}}}' % (formatted_object_names),
						'variables':{},
					}
				}
			)
			action_response = self.send_aura_bulk(action).actions_responses[0]
			if not action_response.is_success():
				logger.error('Error while retrieving field names with GraphQL')
				return None

			objects_infos = filter(None, action_response.return_value['data']['uiapi']['objectInfos'])
			object_fields_map.update({
				x['ApiName']: {
					'fields': [
						y['ApiName'] for y in x['fields']
						if y['dataType'] not in banned_types and y['ApiName'] not in banned_fields
					],
					'leaf_fields': [
						y['ApiName'] for y in x['fields']
						if y['dataType'] in leaf_types and y['ApiName'] not in banned_fields
					]
				}
				for x in objects_infos
			})
		return object_fields_map

	def get_object_count_graphql(self, objects, make_chunks=True):
		logger.verbose("Counting number of records for each objects using GraphQL")
		chunk_size = 10 if make_chunks else 1 #Can be chunk in block of 10 per action, so 10*100 objects in one aura request
		objects_chunks = [objects[i:i+chunk_size] for i in range(0, len(objects), chunk_size)]
		actions_responses = []
		for chunk in objects_chunks:
			#Formatting as follow: "User{totalCount}Account{totalCount}..."
			total_count_query = "".join([f"{object_name}{{totalCount}}" for object_name in chunk])
			action = AuraActionHelper.build_action(
				'1;a',
				'aura://RecordUiController/ACTION$executeGraphQL',
				{
					'queryInput':{
						'operationName':'getCount',
						'query':'query getCount{uiapi{query{%s}}}' % (total_count_query),
						'variables':{},
					}
				}
			)
			try:
				actions_responses += self.send_aura_bulk([action]).actions_responses
			except requests.exceptions.ReadTimeout:
				logger.error("Timeout when trying to count records, one object might have too many records, counting object records one by one...")
				for obj_name in chunk:
					action = AuraActionHelper.build_action(
						'1;a',
						'aura://RecordUiController/ACTION$executeGraphQL',
						{
							'queryInput':{
								'operationName':'getCount',
								'query':'query getCount{uiapi{query{%s{totalCount}}}}' % (obj_name),
								'variables':{},
							}
						}
					)
					try:
						actions_responses += self.send_aura_bulk([action], chunk_size=1).actions_responses
					except requests.exceptions.ReadTimeout:
						logger.error(f"Timeout when trying to count records of {obj_name}, might have too many records")
						object_count_map = {obj_name:-1}

		object_count_map = {}
		all_failed_chunks = []
		for action_response in actions_responses:
			#Error with graphql are not determined by aura state field
			str_response = json.dumps(action_response.return_value)
			if 'uiapi' in action_response.return_value['data']:
				query_response = action_response.return_value['data']['uiapi']['query']
				for obj_name in query_response.keys():
					if query_response[obj_name]:
						object_count_map[obj_name] = query_response[obj_name]['totalCount']
					elif query_response[obj_name] is None:
						for error in action_response.return_value['errors']:
							if 'OPERATION_TOO_LARGE' in error['message'] and len(error['paths']) == 3 and error['paths'][2] == obj_name:
								logger.verbose(f'{obj_name} caused OPERATION_TOO_LARGE, it likely has too many records, setting count at -1')
								object_count_map[obj_name] = -1
							else:
								logger.debug(f'Ignoring {obj_name} because of: {error["message"]}')
			elif 'ValidationError' in str_response:
				#One object likely cause an issue in the request, we need to send them individually
				if make_chunks:
					error_field_regex = r'FieldUndefined:[^\'"]+[\'"]([^\'"]+)[\'"]'
					if error_fields := re.findall(error_field_regex, str_response):
						failed_chunks = [chunk for chunk in objects_chunks for error_field in error_fields if error_field in chunk]
						for failed_chunk in failed_chunks:
							all_failed_chunks += failed_chunk
			else:
				logger.debug("Unhandled error when getting total count for objects with GraphQL: "+json.dumps(action_response.return_value))
		if all_failed_chunks:
			#Send the failed objects individually by calling the func again and not making chunks
			logger.verbose(f'Resending failed chunks while counting records with GraphQL: {all_failed_chunks}')
			failed_chunks_count_map = self.get_object_count_graphql(all_failed_chunks, make_chunks=False)
			object_count_map.update(failed_chunks_count_map)
		return object_count_map

	def _build_gql_fields_query(self, fields, leaf_fields):
		parts = []
		leaf_set = set(leaf_fields)
		for f in fields:
			if f in leaf_set:
				parts.append(f)
			else:
				parts.append(f"{f}{{value}}")
		return " ".join(parts)

	def _fetch_gql_records_for_object(self, object_name, fields, leaf_fields, page_size=50, fetch_all=False):
		all_records = []
		fields_query = self._build_gql_fields_query(fields, leaf_fields)
		cursor = None
		has_next = True

		while has_next:
			after_clause = f',after:"{cursor}"' if cursor else ''
			query = f'query getData{{uiapi{{query{{{object_name}(first:{page_size}{after_clause}){{edges{{node{{{fields_query}}}}}pageInfo{{hasNextPage,endCursor}}}}}}}}}}'

			action = AuraActionHelper.build_action(
				object_name,
				'aura://RecordUiController/ACTION$executeGraphQL',
				{'queryInput':{'operationName':'getData','query':query,'variables':{}}}
			)

			action_response = self.send_aura_bulk(action).actions_responses[0]
			if not action_response.is_success():
				return None, action_response.error_message if action_response.is_error() else 'Unknown error'

			return_value = action_response.return_value

			if 'errors' in return_value and return_value['errors']:
				logger.debug(f'GraphQL errors for {object_name}: {return_value["errors"]}')
				bad_fields = set()
				for error in return_value.get('errors', []):
					msg = error.get('message', '')
					if 'SubselectionNotAllowed' in msg:
						match = re.search(r'/node/(\w+)', msg)
						if match:
							bad_fields.add(match.group(1))
					elif 'FieldUndefined' in msg:
						bad = re.findall(r"'([^']+)'", msg)
						bad_fields.update(bad)

				if bad_fields:
					logger.verbose(f'Retrying {object_name} moving problematic fields to leaf: {bad_fields}')
					new_leaf = list(set(leaf_fields) | bad_fields)
					remaining = [f for f in fields if f not in bad_fields or f in bad_fields]
					return self._fetch_gql_records_for_object(object_name, remaining, new_leaf, page_size, fetch_all)

			if 'data' in return_value and 'uiapi' in return_value.get('data', {}):
				query_data = return_value['data']['uiapi']['query'].get(object_name)
				if query_data and 'edges' in query_data:
					for edge in query_data['edges']:
						node = edge.get('node', {})
						record = {}
						for field_name, field_data in node.items():
							if isinstance(field_data, dict) and 'value' in field_data:
								record[field_name] = field_data['value']
							elif not isinstance(field_data, dict):
								record[field_name] = field_data
						if record:
							all_records.append(record)

					page_info = query_data.get('pageInfo', {})
					if fetch_all and page_info.get('hasNextPage', False):
						cursor = page_info.get('endCursor')
						logger.verbose(f'Fetched {len(all_records)} records so far for {object_name}, fetching next page...')
					else:
						has_next = False
				else:
					has_next = False
			else:
				has_next = False

		return all_records, None

	def get_records_graphql(self, objects, records_per_action=100, fetch_all=False):
		results = {}

		object_fields_map = self.get_graphql_fields_for_objects(objects)
		uiapi_objects = list(object_fields_map.keys())
		logger.info(f"{len(uiapi_objects)} objects accessible with GraphQL through uiapi")

		if fetch_all:
			logger.info("Fetching ALL records (pagination enabled) - this may take a while")
		else:
			logger.info("Hang tight - this may take a while")

		object_count_map = self.get_object_count_graphql(uiapi_objects)
		objects_with_records = [object_name for object_name in object_count_map if object_count_map[object_name] != 0]
		results = {k: {'records': [], 'total_count': v} for k, v in object_count_map.items() if v != 0}
		logger.info(f"{len(objects_with_records)} objects with records for a total of {sum(object_count_map.values())} records")

		for object_name in objects_with_records:
			field_info = object_fields_map.get(object_name, {})
			fields = field_info.get('fields', [])
			leaf_fields = field_info.get('leaf_fields', [])
			if not fields:
				logger.debug(f'No fields found for {object_name}, skipping data fetch')
				continue

			logger.verbose(f'Fetching records for {object_name} ({object_count_map[object_name]} records, {len(fields)} fields)')
			page_size = min(records_per_action, 50)

			try:
				records, error = self._fetch_gql_records_for_object(object_name, fields, leaf_fields, page_size, fetch_all)
				if records is not None:
					if records:
						logger.verbose(f'Retrieved {len(records)} records for {object_name} via GraphQL')
					results[object_name]['records'] = records
				else:
					logger.debug(f'Error fetching records for {object_name}: {error}')
			except Exception:
				logger.error(f'Error while fetching GraphQL records for {object_name}')
				logger.debug(traceback.format_exc())

		return results

	def get_custom_controllers(self):
		# Ignore query params
		parsed_url = urlparse(self.app)
		req_url = f'{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}'
		resp = self.session.get(req_url)
		response_text = resp.text
		custom_controllers = {}
		endpoint_pattern = r'src="([^"]*)"'
		auracmp_pattern = r'/auraCmdDef\?[^"\']+'
		custom_controller_pattern = r'apex://[a-zA-Z0-9_-]+/ACTION\$[a-zA-Z0-9_-]+'

		# Find all URLs using the regular expression pattern
		endpoints = re.findall(endpoint_pattern, response_text) + re.findall(auracmp_pattern, response_text)
		logger.verbose('Endpoints that could contain information about custom controllers discovered, analyzing them')
		logger.debug(endpoints)

		found = False
		for endpoint in endpoints:
			if not 'http:' in endpoint and not 'https:' in endpoint:
				endpoint_url = f'{parsed_url.scheme}://{parsed_url.netloc}{endpoint}'
			else:
				endpoint_url = endpoint

			try:
				resp = self.session.get(endpoint_url)
				response_text = resp.text
				endpoint_controllers = re.findall(custom_controller_pattern, response_text)
				if endpoint_controllers:
					custom_controllers[endpoint_url] = endpoint_controllers if endpoint_url not in custom_controllers else list(set(custom_controllers[endpoint_url] + endpoint_controllers))
			except:
				logger.debug(f'Error when processing URL {endpoint_url} during custom controllers check')

		if len(custom_controllers) == 0:
			logger.error('Did not find any custom controllers')
		else:
			logger.warning(f'Found {sum([len(v) for v in custom_controllers.values()])} custom controllers')

		return custom_controllers


	def build_soap_message(self, body):
		sid = self.session.cookies.get("sid")
		xml_header = '<?xml version="1.0" encoding="utf-8"?>'
		soap_env_header = '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:tns="http://soap.sforce.com/2006/04/metadata">'
		soap_session = f'<soapenv:Header><tns:SessionHeader><tns:sessionId>{sid}</tns:sessionId></tns:SessionHeader></soapenv:Header>'
		soap_env_footer = '</soapenv:Envelope>'
		return f'{xml_header}{soap_env_header}{soap_session}{body}{soap_env_footer}'


	def check_soap_api_enabled(self):
		logger.verbose('Checking if SOAP API is exposed (Require API enabled permission)')
		try:
			#Need to see if there is a way to get latest version
			soap_req = self.session.post(f'{self.url}/services/Soap/u/35.0', headers={'Content-Type':'text/xml', 'SOAPAction': 'Empty'})
			if soap_req.status_code == 500 and 'text/xml' in soap_req.headers['Content-Type']:
				logger.info('SOAP API seems to be enabled, may require username and password authentication')
			else:
				logger.info('SOAP API does not seem to be exposed')
		except:
			logger.error('Error while querying the SOAP API')
			logger.debug(traceback.format_exc())


	def check_rest_api_enabled(self):
		logger.verbose('Checking if REST API is exposed (Require API enabled permission)')
		latest_rest_url = None
		try:
			latest_rest_url_req = self.session.get(f'{self.url}/services/data')
			latest_rest_url = latest_rest_url_req.json()[-1]['url']
			self.rest_api_url = latest_rest_url
			logger.verbose(f'Checking REST url using URL: {self.url}{latest_rest_url}')
		except:
			logger.error('Error while retrieving REST URL for latest version')
			logger.debug(traceback.format_exc())
			return False
		headers = {'Authorization': f'Bearer {self.session.cookies.get("sid")}'}
		try:
			rest_req = self.session.get(f'{self.url}{latest_rest_url}', headers=headers)
			if rest_req.status_code == 200:
				self.rest_api_accessible = True
				logger.info(f'REST API is accessible with the provided SID: {self.session.cookies.get("sid")}')
				return True
			else:
				logger.info(f'REST API is not accessible using the provided SID: {self.session.cookies.get("sid")}')
				return False
		except:
			logger.debug(traceback.format_exc())
			logger.error('Error while querying the REST request for latest version')
		return False


	def _rest_headers(self):
		sid = self.session.cookies.get('sid')
		return {**self.headers, 'Authorization': f'Bearer {sid}'} if sid else None

	def invoke_custom_controllers(self, custom_controllers):
		"""Invoke discovered custom Apex controllers with empty params to extract data"""
		results = {}

		unique_actions = list(set(
			action for actions in custom_controllers.values() for action in actions
		))

		if not unique_actions:
			logger.info('No custom controllers to invoke')
			return results

		logger.info(f'Invoking {len(unique_actions)} custom controller actions')

		actions = [
			AuraActionHelper.build_action(descriptor, descriptor, {})
			for descriptor in unique_actions
		]

		actions_responses = self.send_aura_bulk(actions).actions_responses
		for action_response in actions_responses:
			descriptor = action_response.id
			if action_response.is_success() and action_response.return_value is not None:
				results[descriptor] = action_response.return_value
				logger.warning(f'Custom controller returned data: {descriptor}')
			elif action_response.is_error():
				logger.debug(f'Custom controller error for {descriptor}: {action_response.error_message}')

		if results:
			logger.warning(f'{len(results)} custom controllers returned data')
		else:
			logger.info('No custom controllers returned data with empty parameters')

		return results


	def download_content_files(self, record_sources, output_dir):
		"""Download actual file blobs from ContentVersion and ContentDocument records"""
		content_version_ids = set()
		content_document_ids = set()

		for source in record_sources:
			if 'ContentVersion' in source:
				for record in source['ContentVersion'].get('records', []):
					if isinstance(record, dict):
						rid = record.get('Id') or record.get('id')
						if rid:
							content_version_ids.add(rid)
			if 'ContentDocument' in source:
				for record in source['ContentDocument'].get('records', []):
					if isinstance(record, dict):
						vid = record.get('LatestPublishedVersionId')
						if vid:
							content_version_ids.add(vid)
						rid = record.get('Id') or record.get('id')
						if rid:
							content_document_ids.add(rid)

		total = len(content_version_ids) + len(content_document_ids)
		if total == 0:
			logger.info('No ContentVersion/ContentDocument records found to download')
			return []

		logger.info(f'Attempting to download {total} files')

		files_dir = os.path.join(output_dir, 'files')
		os.makedirs(files_dir, exist_ok=True)

		downloaded = []

		for cv_id in content_version_ids:
			try:
				url = f'{self.url}/sfc/servlet.shepherd/version/download/{cv_id}'
				resp = self.session.get(url, headers=self.headers, allow_redirects=True, timeout=120, stream=True)
				if resp.status_code == 200 and int(resp.headers.get('Content-Length', '1')) > 0:
					content_disp = resp.headers.get('Content-Disposition', '')
					filename = cv_id
					m = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)', content_disp)
					if m:
						safe = re.sub(r'[^\w.\-]', '_', m.group(1).strip())
						filename = f'{cv_id}_{safe}'

					filepath = os.path.join(files_dir, filename)
					size = 0
					with open(filepath, 'wb') as f:
						for chunk in resp.iter_content(chunk_size=8192):
							f.write(chunk)
							size += len(chunk)
					downloaded.append({'id': cv_id, 'type': 'ContentVersion', 'filename': filename, 'size': size})
					logger.verbose(f'Downloaded: {filename} ({size} bytes)')
				else:
					logger.debug(f'Could not download ContentVersion {cv_id}: HTTP {resp.status_code}')
			except Exception:
				logger.debug(f'Error downloading ContentVersion {cv_id}')
				logger.debug(traceback.format_exc())

		for cd_id in content_document_ids:
			try:
				url = f'{self.url}/sfc/servlet.shepherd/document/download/{cd_id}'
				resp = self.session.get(url, headers=self.headers, allow_redirects=True, timeout=120, stream=True)
				if resp.status_code == 200 and int(resp.headers.get('Content-Length', '1')) > 0:
					content_disp = resp.headers.get('Content-Disposition', '')
					filename = cd_id
					m = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)', content_disp)
					if m:
						safe = re.sub(r'[^\w.\-]', '_', m.group(1).strip())
						filename = f'{cd_id}_{safe}'

					filepath = os.path.join(files_dir, filename)
					size = 0
					with open(filepath, 'wb') as f:
						for chunk in resp.iter_content(chunk_size=8192):
							f.write(chunk)
							size += len(chunk)
					downloaded.append({'id': cd_id, 'type': 'ContentDocument', 'filename': filename, 'size': size})
					logger.verbose(f'Downloaded: {filename} ({size} bytes)')
				else:
					logger.debug(f'Could not download ContentDocument {cd_id}: HTTP {resp.status_code}')
			except Exception:
				logger.debug(f'Error downloading ContentDocument {cd_id}')
				logger.debug(traceback.format_exc())

		if downloaded:
			logger.warning(f'Downloaded {len(downloaded)} files to {files_dir}')
			manifest = os.path.join(files_dir, 'manifest.json')
			with open(manifest, 'w') as f:
				json.dump(downloaded, f, indent=2)
		else:
			logger.info('No files could be downloaded')

		return downloaded


	def extract_via_rest_api(self, objects):
		"""Extract all object data via SOQL through the REST API"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not accessible, skipping SOQL extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			logger.info('No SID cookie available, skipping REST API extraction')
			return results

		logger.info(f'Extracting data via REST API SOQL for {len(objects)} objects')

		for object_name in objects:
			try:
				describe_resp = self.session.get(
					f'{self.url}{self.rest_api_url}/sobjects/{object_name}/describe',
					headers=auth_headers, timeout=30
				)
				if describe_resp.status_code != 200:
					logger.debug(f'Cannot describe {object_name} via REST: HTTP {describe_resp.status_code}')
					continue

				describe = describe_resp.json()
				if not describe.get('queryable', False):
					logger.debug(f'{object_name} is not queryable via REST')
					continue

				fields = [
					f['name'] for f in describe.get('fields', [])
					if f.get('type') not in ('address', 'location')
				]
				if not fields:
					continue

				query = f"SELECT {','.join(fields)} FROM {object_name}"
				all_records = []
				query_url = f"{self.url}{self.rest_api_url}/query?q={quote(query)}"
				total_size = 0

				while query_url:
					try:
						resp = self.session.get(query_url, headers=auth_headers, timeout=60)
						if resp.status_code != 200:
							logger.debug(f'SOQL query failed for {object_name}: HTTP {resp.status_code}')
							break

						data = resp.json()
						total_size = data.get('totalSize', total_size)
						records = data.get('records', [])
						for r in records:
							r.pop('attributes', None)
						all_records.extend(records)

						next_url = data.get('nextRecordsUrl')
						query_url = f"{self.url}{next_url}" if next_url else None

						if next_url:
							logger.verbose(f'REST pagination for {object_name}: {len(all_records)}/{total_size}')
					except Exception:
						logger.debug(f'Error during REST pagination for {object_name}')
						logger.debug(traceback.format_exc())
						break

				if all_records:
					results[object_name] = {'records': all_records, 'total_count': total_size}
					logger.verbose(f'REST API: {len(all_records)} records for {object_name}')
			except Exception:
				logger.debug(f'Error extracting {object_name} via REST')
				logger.debug(traceback.format_exc())

		logger.info(f'REST API extracted data for {len(results)} objects')
		return results


	def search_records(self, search_terms=None):
		"""Search across objects using SOSL (REST) and Aura search actions"""
		results = {'sosl': {}, 'aura': {}}

		if search_terms is None:
			search_terms = ['test', 'admin', 'password', 'secret', 'key', 'token',
							'credential', 'internal', 'confidential', 'private']

		# SOSL via REST API (authenticated)
		if self.rest_api_accessible and self.rest_api_url:
			auth_headers = self._rest_headers()
			if auth_headers:
				logger.info(f'Running SOSL searches for {len(search_terms)} terms')
				for term in search_terms:
					try:
						sosl = f'FIND {{{term}}} IN ALL FIELDS'
						resp = self.session.get(
							f'{self.url}{self.rest_api_url}/search?q={quote(sosl)}',
							headers=auth_headers, timeout=30
						)
						if resp.status_code == 200:
							records = resp.json().get('searchRecords', [])
							if records:
								for r in records:
									r.pop('attributes', None)
								results['sosl'][term] = records
								logger.verbose(f'SOSL "{term}": {len(records)} results')
						else:
							logger.debug(f'SOSL search failed for "{term}": HTTP {resp.status_code}')
					except Exception:
						logger.debug(f'Error during SOSL search for "{term}"')

		# Aura-based search (works for Guest users)
		logger.info(f'Running Aura searches for {len(search_terms)} terms')
		for term in search_terms:
			action = AuraActionHelper.build_action(
				f'search_{term}',
				'serviceComponent://ui.force.components.controllers.search2.SearchController/ACTION$getSearchResults',
				{'searchString': term, 'pageSize': 100, 'scopeEntityName': None, 'searchType': 'GENERAL'}
			)
			try:
				responses = self.send_aura_bulk(action).actions_responses
				for resp in responses:
					if resp.is_success() and resp.return_value:
						results['aura'][term] = resp.return_value
						logger.verbose(f'Aura search "{term}": returned data')
					elif resp.is_error():
						logger.debug(f'Aura search "{term}" error: {resp.error_message}')
			except Exception:
				logger.debug(f'Error during Aura search for "{term}"')

		sosl_count = sum(len(v) for v in results['sosl'].values())
		aura_count = len(results['aura'])
		if sosl_count + aura_count > 0:
			logger.warning(f'Search returned results: {sosl_count} SOSL records, {aura_count} Aura result sets')
		else:
			logger.info('No search results found')

		return results


	def get_records_by_ids(self, record_ids):
		"""Fetch full record details by ID using Aura getRecord"""
		results = {}

		if not record_ids:
			return results

		logger.info(f'Fetching {len(record_ids)} records by ID')

		actions = [
			AuraActionHelper.build_action(
				record_id,
				'serviceComponent://ui.force.components.controllers.detail.DetailController/ACTION$getRecord',
				{
					'recordId': record_id,
					'record': None,
					'inContextOfComponent': '',
					'mode': 'VIEW',
					'layoutType': 'FULL',
					'defaultFieldValues': None,
					'navigationLocation': 'LIST_VIEW_ROW'
				}
			)
			for record_id in record_ids
		]

		actions_responses = self.send_aura_bulk(actions).actions_responses
		for action_response in actions_responses:
			record_id = action_response.id
			if action_response.is_success() and action_response.return_value:
				results[record_id] = action_response.return_value
				logger.verbose(f'Retrieved full record for {record_id}')
			elif action_response.is_error():
				logger.debug(f'Could not get record {record_id}: {action_response.error_message}')

		if results:
			logger.info(f'Retrieved {len(results)} records by ID')

		return results


	def extract_related_records(self, *record_sources):
		"""Follow relationship/lookup fields to find records not discovered via list enumeration"""
		sf_id_pattern = re.compile(r'^[a-zA-Z0-9]{15}$|^[a-zA-Z0-9]{18}$')
		known_ids = set()
		referenced_ids = set()

		for source in record_sources:
			for obj_name, data in source.items():
				for record in data.get('records', []):
					if not isinstance(record, dict):
						continue
					for key, value in record.items():
						if not isinstance(value, str) or not sf_id_pattern.match(value):
							continue
						if key.lower() == 'id':
							known_ids.add(value)
						else:
							referenced_ids.add(value)

		new_ids = list(referenced_ids - known_ids)
		if not new_ids:
			logger.info('No new related record IDs to fetch')
			return {}

		cap = 1000
		if len(new_ids) > cap:
			logger.warning(f'Capping related record fetch at {cap} (found {len(new_ids)})')
			new_ids = new_ids[:cap]

		logger.info(f'Extracting {len(new_ids)} related records by ID')
		return self.get_records_by_ids(new_ids)


	def get_chatter_feeds(self):
		"""Extract Chatter/feed data via Aura and REST API"""
		results = {}

		feed_descriptors = [
			('news', {'feedType': 'NEWS', 'pageSize': 50}),
			('home', {'feedType': 'HOME', 'pageSize': 50}),
			('company', {'feedType': 'COMPANY', 'pageSize': 50}),
		]

		for feed_name, params in feed_descriptors:
			try:
				action = AuraActionHelper.build_action(
					f'feed_{feed_name}',
					'serviceComponent://ui.chatter.components.aura.components.forceChatter.feedContainer.FeedContainerController/ACTION$getFeed',
					params
				)
				resp = self.send_aura_bulk(action).actions_responses
				if resp and resp[0].is_success() and resp[0].return_value:
					results[f'aura_{feed_name}'] = resp[0].return_value
					logger.verbose(f'Retrieved Chatter feed via Aura: {feed_name}')
			except Exception:
				logger.debug(f'Error getting Aura Chatter feed {feed_name}')

		if self.rest_api_accessible and self.rest_api_url:
			auth_headers = self._rest_headers()
			if auth_headers:
				rest_feeds = [
					('company_feed', '/chatter/feeds/company/feed-elements'),
					('news_feed', '/chatter/feeds/news/me/feed-elements'),
					('groups', '/chatter/groups'),
				]
				for name, endpoint in rest_feeds:
					try:
						resp = self.session.get(
							f'{self.url}{self.rest_api_url}{endpoint}',
							headers=auth_headers, timeout=30
						)
						if resp.status_code == 200:
							data = resp.json()
							if data:
								results[f'rest_{name}'] = data
								logger.verbose(f'Retrieved Chatter via REST: {name}')
					except Exception:
						logger.debug(f'Error getting Chatter via REST: {name}')

		if results:
			logger.warning(f'Retrieved Chatter data from {len(results)} sources')
		else:
			logger.info('No Chatter data accessible')

		return results


	def get_reports_data(self):
		"""Discover and extract accessible report data via REST API"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping report extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/analytics/reports',
				headers=auth_headers, timeout=30
			)
			if resp.status_code != 200:
				logger.info('Reports API not accessible')
				return results

			reports_list = resp.json()
			logger.info(f'Found {len(reports_list)} reports, attempting extraction')

			for report_meta in reports_list:
				report_id = report_meta.get('id')
				report_name = report_meta.get('name', report_id)
				try:
					data_resp = self.session.get(
						f'{self.url}{self.rest_api_url}/analytics/reports/{report_id}',
						headers=auth_headers, timeout=60
					)
					if data_resp.status_code == 200:
						results[report_name] = data_resp.json()
						logger.verbose(f'Extracted report: {report_name}')
				except Exception:
					logger.debug(f'Error extracting report {report_name}')
		except Exception:
			logger.debug('Error listing reports')
			logger.debug(traceback.format_exc())

		if results:
			logger.warning(f'Extracted data from {len(results)} reports')

		return results


	def get_dashboards_data(self):
		"""Discover and extract accessible dashboard data via REST API"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/analytics/dashboards',
				headers=auth_headers, timeout=30
			)
			if resp.status_code != 200:
				logger.info('Dashboards API not accessible')
				return results

			dashboards_data = resp.json()
			dashboards_list = dashboards_data.get('dashboards', dashboards_data) if isinstance(dashboards_data, dict) else dashboards_data
			if not isinstance(dashboards_list, list):
				dashboards_list = []

			logger.info(f'Found {len(dashboards_list)} dashboards')

			for dash in dashboards_list:
				if not isinstance(dash, dict):
					continue
				dash_id = dash.get('id')
				dash_name = dash.get('name', dash_id)
				if not dash_id:
					continue
				try:
					data_resp = self.session.get(
						f'{self.url}{self.rest_api_url}/analytics/dashboards/{dash_id}',
						headers=auth_headers, timeout=60
					)
					if data_resp.status_code == 200:
						results[dash_name] = data_resp.json()
						logger.verbose(f'Extracted dashboard: {dash_name}')
				except Exception:
					logger.debug(f'Error extracting dashboard {dash_name}')
		except Exception:
			logger.debug('Error listing dashboards')
			logger.debug(traceback.format_exc())

		if results:
			logger.warning(f'Extracted data from {len(results)} dashboards')

		return results


	def extract_tooling_api(self, output_dir):
		"""Extract Apex source code, triggers, VF pages, and Lightning components via Tooling API"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping Tooling API extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		tooling_base = f'{self.url}{self.rest_api_url}/tooling'

		artifact_types = [
			('ApexClass', 'Id,Name,Body,ApiVersion,Status,LengthWithoutComments'),
			('ApexTrigger', 'Id,Name,Body,ApiVersion,Status,TableEnumOrId'),
			('ApexPage', 'Id,Name,Markup,ApiVersion,Description'),
			('ApexComponent', 'Id,Name,Markup,ApiVersion,Description'),
			('AuraDefinitionBundle', 'Id,DeveloperName,MasterLabel,ApiVersion,Description'),
			('LightningComponentBundle', 'Id,DeveloperName,MasterLabel,ApiVersion,Description'),
		]

		for artifact_type, fields in artifact_types:
			try:
				query = f'SELECT {fields} FROM {artifact_type}'
				resp = self.session.get(
					f'{tooling_base}/query?q={quote(query)}',
					headers=auth_headers, timeout=60
				)
				if resp.status_code != 200:
					logger.debug(f'Tooling query failed for {artifact_type}: HTTP {resp.status_code}')
					continue

				data = resp.json()
				records = data.get('records', [])
				for r in records:
					r.pop('attributes', None)

				all_records = list(records)
				next_url = data.get('nextRecordsUrl')
				while next_url:
					try:
						resp = self.session.get(f'{self.url}{next_url}', headers=auth_headers, timeout=60)
						if resp.status_code != 200:
							break
						page = resp.json()
						page_records = page.get('records', [])
						for r in page_records:
							r.pop('attributes', None)
						all_records.extend(page_records)
						next_url = page.get('nextRecordsUrl')
					except Exception:
						break

				if all_records:
					results[artifact_type] = all_records
					logger.warning(f'Tooling API: extracted {len(all_records)} {artifact_type} records')
			except Exception:
				logger.debug(f'Error querying Tooling API for {artifact_type}')
				logger.debug(traceback.format_exc())

		# Fetch AuraDefinition bodies (individual component files within bundles)
		if 'AuraDefinitionBundle' in results:
			bundle_ids = [r['Id'] for r in results['AuraDefinitionBundle'] if r.get('Id')]
			if bundle_ids:
				try:
					aura_defs = []
					for i in range(0, len(bundle_ids), 20):
						batch = bundle_ids[i:i+20]
						id_list = "','".join(batch)
						query = f"SELECT Id,AuraDefinitionBundleId,DefType,Format,Source FROM AuraDefinition WHERE AuraDefinitionBundleId IN ('{id_list}')"
						resp = self.session.get(
							f'{tooling_base}/query?q={quote(query)}',
							headers=auth_headers, timeout=60
						)
						if resp.status_code == 200:
							records = resp.json().get('records', [])
							for r in records:
								r.pop('attributes', None)
							aura_defs.extend(records)
					if aura_defs:
						results['AuraDefinition'] = aura_defs
						logger.warning(f'Tooling API: extracted {len(aura_defs)} AuraDefinition source files')
				except Exception:
					logger.debug('Error fetching AuraDefinition bodies')

		# Write source code to files
		if results and output_dir:
			source_dir = os.path.join(output_dir, 'source_code')
			os.makedirs(source_dir, exist_ok=True)
			for artifact_type, records in results.items():
				type_dir = os.path.join(source_dir, artifact_type)
				os.makedirs(type_dir, exist_ok=True)
				for record in records:
					name = record.get('Name') or record.get('DeveloperName') or record.get('Id', 'unknown')
					body = record.get('Body') or record.get('Markup') or record.get('Source')
					if body:
						ext = '.cls' if artifact_type == 'ApexClass' else '.trigger' if artifact_type == 'ApexTrigger' else '.page' if artifact_type == 'ApexPage' else '.cmp' if artifact_type in ('ApexComponent', 'AuraDefinition') else '.txt'
						if artifact_type == 'AuraDefinition':
							def_type = record.get('DefType', '')
							name = f"{record.get('AuraDefinitionBundleId', 'unknown')}_{def_type}_{name}"
						safe_name = re.sub(r'[^\w.\-]', '_', str(name))
						filepath = os.path.join(type_dir, f'{safe_name}{ext}')
						with open(filepath, 'w', encoding='utf-8') as f:
							f.write(body)
			logger.warning(f'Source code written to {source_dir}')

		if results:
			total = sum(len(v) for v in results.values())
			logger.warning(f'Tooling API: extracted {total} artifacts across {len(results)} types')
		else:
			logger.info('Tooling API: no artifacts extracted')

		return results


	def download_static_resources(self, all_records_sources, output_dir):
		"""Download actual Static Resource file content"""
		static_resource_records = []

		for source in all_records_sources:
			if 'StaticResource' in source:
				static_resource_records.extend(source['StaticResource'].get('records', []))

		if not static_resource_records:
			logger.info('No StaticResource records found to download')
			return []

		downloaded = []
		sr_dir = os.path.join(output_dir, 'static_resources')
		os.makedirs(sr_dir, exist_ok=True)

		# Try via REST API body field
		if self.rest_api_accessible and self.rest_api_url:
			auth_headers = self._rest_headers()
			if auth_headers:
				sr_ids = [r.get('Id') or r.get('id') for r in static_resource_records if r.get('Id') or r.get('id')]
				unique_ids = list(set(sr_ids))
				logger.info(f'Downloading {len(unique_ids)} Static Resources via REST API')
				for sr_id in unique_ids:
					try:
						resp = self.session.get(
							f'{self.url}{self.rest_api_url}/sobjects/StaticResource/{sr_id}/Body',
							headers=auth_headers, timeout=60, stream=True
						)
						if resp.status_code == 200:
							name = sr_id
							for r in static_resource_records:
								if (r.get('Id') or r.get('id')) == sr_id:
									name = r.get('Name') or r.get('name') or sr_id
									break
							safe_name = re.sub(r'[^\w.\-]', '_', str(name))
							content_type = resp.headers.get('Content-Type', '')
							ext = '.zip' if 'zip' in content_type else '.js' if 'javascript' in content_type else '.css' if 'css' in content_type else '.bin'
							filepath = os.path.join(sr_dir, f'{safe_name}{ext}')
							size = 0
							with open(filepath, 'wb') as f:
								for chunk in resp.iter_content(chunk_size=8192):
									f.write(chunk)
									size += len(chunk)
							downloaded.append({'id': sr_id, 'name': name, 'filename': f'{safe_name}{ext}', 'size': size})
							logger.verbose(f'Downloaded StaticResource: {name} ({size} bytes)')
					except Exception:
						logger.debug(f'Error downloading StaticResource {sr_id}')
				if downloaded:
					logger.warning(f'Downloaded {len(downloaded)} Static Resources to {sr_dir}')
					manifest = os.path.join(sr_dir, 'manifest.json')
					with open(manifest, 'w') as f:
						json.dump(downloaded, f, indent=2)
				return downloaded

		# Fallback: try direct URL for publicly accessible resources
		for record in static_resource_records:
			name = record.get('Name') or record.get('name')
			if not name:
				continue
			try:
				url = f'{self.url}/resource/{name}'
				resp = self.session.get(url, headers=self.headers, allow_redirects=True, timeout=30, stream=True)
				if resp.status_code == 200 and int(resp.headers.get('Content-Length', '0')) > 0:
					safe_name = re.sub(r'[^\w.\-]', '_', str(name))
					content_type = resp.headers.get('Content-Type', '')
					ext = '.zip' if 'zip' in content_type else '.js' if 'javascript' in content_type else '.css' if 'css' in content_type else '.bin'
					filepath = os.path.join(sr_dir, f'{safe_name}{ext}')
					size = 0
					with open(filepath, 'wb') as f:
						for chunk in resp.iter_content(chunk_size=8192):
							f.write(chunk)
							size += len(chunk)
					downloaded.append({'id': record.get('Id', ''), 'name': name, 'filename': f'{safe_name}{ext}', 'size': size})
					logger.verbose(f'Downloaded StaticResource: {name} ({size} bytes)')
			except Exception:
				logger.debug(f'Error downloading StaticResource {name}')

		if downloaded:
			logger.warning(f'Downloaded {len(downloaded)} Static Resources to {sr_dir}')
			manifest = os.path.join(sr_dir, 'manifest.json')
			with open(manifest, 'w') as f:
				json.dump(downloaded, f, indent=2)
		else:
			logger.info('No Static Resources could be downloaded')

		return downloaded


	def extract_metadata_describe(self, objects):
		"""Extract full object and field schema metadata via describe"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping metadata describe')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		# Global describe
		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/sobjects',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				results['_global'] = resp.json()
				logger.verbose(f'Global describe: {len(results["_global"].get("sobjects", []))} objects')
		except Exception:
			logger.debug('Error fetching global describe')

		logger.info(f'Describing {len(objects)} objects (fields, relationships, picklists)')
		for object_name in objects:
			try:
				resp = self.session.get(
					f'{self.url}{self.rest_api_url}/sobjects/{object_name}/describe',
					headers=auth_headers, timeout=30
				)
				if resp.status_code == 200:
					results[object_name] = resp.json()
				else:
					logger.debug(f'Cannot describe {object_name}: HTTP {resp.status_code}')
			except Exception:
				logger.debug(f'Error describing {object_name}')

		if len(results) > 1:
			logger.warning(f'Described {len(results) - 1} objects (full field/relationship metadata)')
		else:
			logger.info('No object metadata extracted')

		return results


	def extract_knowledge_articles(self):
		"""Extract Knowledge Base articles via REST API and Aura"""
		results = {'rest': [], 'aura': []}

		# REST API Knowledge
		if self.rest_api_accessible and self.rest_api_url:
			auth_headers = self._rest_headers()
			if auth_headers:
				try:
					resp = self.session.get(
						f'{self.url}{self.rest_api_url}/support/knowledgeArticles',
						headers=auth_headers, timeout=30
					)
					if resp.status_code == 200:
						articles_data = resp.json()
						articles = articles_data.get('articles', [])
						logger.info(f'Found {len(articles)} Knowledge articles via REST')

						for article in articles:
							article_id = article.get('id')
							if article_id:
								try:
									detail = self.session.get(
										f'{self.url}{self.rest_api_url}/support/knowledgeArticles/{article_id}',
										headers=auth_headers, timeout=30
									)
									if detail.status_code == 200:
										results['rest'].append(detail.json())
										logger.verbose(f'Retrieved article: {article.get("title", article_id)}')
								except Exception:
									logger.debug(f'Error fetching article {article_id}')
					else:
						logger.debug(f'Knowledge API not available: HTTP {resp.status_code}')
				except Exception:
					logger.debug('Error listing Knowledge articles')

				# Also try SOQL for Knowledge objects (they end in __kav)
				try:
					global_describe = self.session.get(
						f'{self.url}{self.rest_api_url}/sobjects',
						headers=auth_headers, timeout=30
					)
					if global_describe.status_code == 200:
						sobjects = global_describe.json().get('sobjects', [])
						kav_objects = [s['name'] for s in sobjects if s['name'].endswith('__kav') and s.get('queryable')]
						for kav in kav_objects:
							try:
								describe_resp = self.session.get(
									f'{self.url}{self.rest_api_url}/sobjects/{kav}/describe',
									headers=auth_headers, timeout=30
								)
								if describe_resp.status_code != 200:
									continue
								fields = [f['name'] for f in describe_resp.json().get('fields', []) if f.get('type') not in ('address', 'location')]
								query = f"SELECT {','.join(fields)} FROM {kav} WHERE PublishStatus='Online' AND Language='en_US'"
								resp = self.session.get(
									f'{self.url}{self.rest_api_url}/query?q={quote(query)}',
									headers=auth_headers, timeout=60
								)
								if resp.status_code == 200:
									records = resp.json().get('records', [])
									for r in records:
										r.pop('attributes', None)
									if records:
										results['rest'].extend(records)
										logger.warning(f'Knowledge: extracted {len(records)} articles from {kav}')
							except Exception:
								logger.debug(f'Error querying {kav}')
				except Exception:
					logger.debug('Error discovering Knowledge objects')

		# Aura-based Knowledge (works for Guest users)
		try:
			action = AuraActionHelper.build_action(
				'knowledge',
				'serviceComponent://ui.force.components.controllers.knowledge.KnowledgeController/ACTION$getArticles',
				{'pageSize': 100, 'pageNumber': 1}
			)
			responses = self.send_aura_bulk(action).actions_responses
			if responses and responses[0].is_success() and responses[0].return_value:
				results['aura'] = responses[0].return_value
				logger.verbose('Retrieved Knowledge articles via Aura')
		except Exception:
			logger.debug('Error fetching Knowledge articles via Aura')

		total = len(results.get('rest', [])) + (1 if results.get('aura') else 0)
		if total:
			logger.warning(f'Knowledge: extracted {len(results["rest"])} REST articles, {"yes" if results.get("aura") else "no"} Aura data')
		else:
			logger.info('No Knowledge articles extracted')

		return results


	def download_legacy_attachments(self, all_records_sources, output_dir):
		"""Download legacy Attachment object files"""
		attachment_ids = set()

		for source in all_records_sources:
			if 'Attachment' in source:
				for record in source['Attachment'].get('records', []):
					if isinstance(record, dict):
						rid = record.get('Id') or record.get('id')
						if rid:
							attachment_ids.add(rid)

		# Also try to query Attachment table via REST if accessible
		if self.rest_api_accessible and self.rest_api_url:
			auth_headers = self._rest_headers()
			if auth_headers:
				try:
					resp = self.session.get(
						f'{self.url}{self.rest_api_url}/query?q={quote("SELECT Id,Name,ContentType,BodyLength FROM Attachment LIMIT 2000")}',
						headers=auth_headers, timeout=60
					)
					if resp.status_code == 200:
						records = resp.json().get('records', [])
						for r in records:
							if r.get('Id'):
								attachment_ids.add(r['Id'])
				except Exception:
					logger.debug('Error querying Attachment objects')

		if not attachment_ids:
			logger.info('No Attachment records found to download')
			return []

		logger.info(f'Downloading {len(attachment_ids)} legacy Attachments')
		att_dir = os.path.join(output_dir, 'attachments')
		os.makedirs(att_dir, exist_ok=True)

		downloaded = []
		for att_id in attachment_ids:
			# Try REST blob endpoint
			if self.rest_api_accessible and self.rest_api_url:
				auth_headers = self._rest_headers()
				if auth_headers:
					try:
						resp = self.session.get(
							f'{self.url}{self.rest_api_url}/sobjects/Attachment/{att_id}/Body',
							headers=auth_headers, timeout=120, stream=True
						)
						if resp.status_code == 200:
							content_disp = resp.headers.get('Content-Disposition', '')
							filename = att_id
							m = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)', content_disp)
							if m:
								safe = re.sub(r'[^\w.\-]', '_', m.group(1).strip())
								filename = f'{att_id}_{safe}'

							filepath = os.path.join(att_dir, filename)
							size = 0
							with open(filepath, 'wb') as f:
								for chunk in resp.iter_content(chunk_size=8192):
									f.write(chunk)
									size += len(chunk)
							downloaded.append({'id': att_id, 'filename': filename, 'size': size})
							logger.verbose(f'Downloaded Attachment: {filename} ({size} bytes)')
							continue
					except Exception:
						pass

			# Fallback: legacy servlet
			try:
				url = f'{self.url}/servlet/servlet.FileDownload?file={att_id}'
				resp = self.session.get(url, headers=self.headers, allow_redirects=True, timeout=120, stream=True)
				if resp.status_code == 200 and int(resp.headers.get('Content-Length', '0')) > 0:
					filepath = os.path.join(att_dir, att_id)
					size = 0
					with open(filepath, 'wb') as f:
						for chunk in resp.iter_content(chunk_size=8192):
							f.write(chunk)
							size += len(chunk)
					downloaded.append({'id': att_id, 'filename': att_id, 'size': size})
					logger.verbose(f'Downloaded Attachment: {att_id} ({size} bytes)')
			except Exception:
				logger.debug(f'Error downloading Attachment {att_id}')

		if downloaded:
			logger.warning(f'Downloaded {len(downloaded)} Attachments to {att_dir}')
			manifest = os.path.join(att_dir, 'manifest.json')
			with open(manifest, 'w') as f:
				json.dump(downloaded, f, indent=2)
		else:
			logger.info('No Attachments could be downloaded')

		return downloaded


	def extract_custom_settings(self):
		"""Extract Custom Settings and Custom Metadata Type records via REST API"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping Custom Settings extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/sobjects',
				headers=auth_headers, timeout=30
			)
			if resp.status_code != 200:
				return results

			sobjects = resp.json().get('sobjects', [])

			# Custom Settings end with __c and have customSetting=true
			custom_settings = [s['name'] for s in sobjects if s.get('customSetting')]
			# Custom Metadata Types end with __mdt
			custom_metadata = [s['name'] for s in sobjects if s['name'].endswith('__mdt') and s.get('queryable')]

			targets = custom_settings + custom_metadata
			if not targets:
				logger.info('No Custom Settings or Custom Metadata Types found')
				return results

			logger.info(f'Extracting {len(custom_settings)} Custom Settings and {len(custom_metadata)} Custom Metadata Types')

			for obj_name in targets:
				try:
					describe_resp = self.session.get(
						f'{self.url}{self.rest_api_url}/sobjects/{obj_name}/describe',
						headers=auth_headers, timeout=30
					)
					if describe_resp.status_code != 200:
						continue

					fields = [f['name'] for f in describe_resp.json().get('fields', []) if f.get('type') not in ('address', 'location')]
					query = f"SELECT {','.join(fields)} FROM {obj_name}"
					resp = self.session.get(
						f'{self.url}{self.rest_api_url}/query?q={quote(query)}',
						headers=auth_headers, timeout=60
					)
					if resp.status_code == 200:
						records = resp.json().get('records', [])
						for r in records:
							r.pop('attributes', None)
						if records:
							results[obj_name] = records
							logger.verbose(f'Extracted {len(records)} records from {obj_name}')
				except Exception:
					logger.debug(f'Error extracting {obj_name}')
		except Exception:
			logger.debug('Error discovering Custom Settings/Metadata')
			logger.debug(traceback.format_exc())

		if results:
			logger.warning(f'Extracted data from {len(results)} Custom Settings/Metadata Types')
		else:
			logger.info('No Custom Settings/Metadata data extracted')

		return results


	def extract_aura_component_defs(self):
		"""Extract Aura component definitions to reveal custom logic and data flows"""
		results = {}

		# Common components and any custom ones
		component_names = [
			'markup://force:recordData',
			'markup://force:recordView',
			'markup://force:recordEdit',
			'markup://forceCommunity:recordView',
			'markup://forceCommunity:richText',
			'markup://forceCommunity:appLauncher',
		]

		# Try to discover custom component names from the app page
		try:
			parsed_url = urlparse(self.app)
			req_url = f'{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}'
			resp = self.session.get(req_url, headers=self.headers)
			custom_pattern = r'markup://c:([a-zA-Z0-9_]+)'
			custom_components = re.findall(custom_pattern, resp.text)
			for comp in set(custom_components):
				component_names.append(f'markup://c:{comp}')
		except Exception:
			logger.debug('Error discovering custom component names')

		unique_names = list(set(component_names))
		logger.info(f'Fetching {len(unique_names)} Aura component definitions')

		actions = [
			AuraActionHelper.build_action(
				name,
				'aura://ComponentDefMapperController/ACTION$getComponentDef',
				{'descriptor': name}
			)
			for name in unique_names
		]

		actions_responses = self.send_aura_bulk(actions).actions_responses
		for action_response in actions_responses:
			comp_name = action_response.id
			if action_response.is_success() and action_response.return_value:
				results[comp_name] = action_response.return_value
				logger.verbose(f'Retrieved component def: {comp_name}')
			elif action_response.is_error():
				logger.debug(f'Component def error for {comp_name}: {action_response.error_message}')

		if results:
			logger.warning(f'Extracted {len(results)} Aura component definitions')
		else:
			logger.info('No Aura component definitions extracted')

		return results


	def discover_apex_rest_endpoints(self):
		"""Discover and probe custom Apex REST endpoints"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping Apex REST discovery')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		# Query for @RestResource annotated classes via Tooling API
		try:
			query = "SELECT Id,Name,Body FROM ApexClass WHERE Body LIKE '%@RestResource%'"
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/tooling/query?q={quote(query)}',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				records = resp.json().get('records', [])
				endpoint_pattern = r'@RestResource\s*\(\s*urlMapping\s*=\s*[\'"]([^\'"]+)[\'"]'
				for record in records:
					body = record.get('Body', '')
					match = re.search(endpoint_pattern, body)
					if match:
						url_mapping = match.group(1)
						results[record['Name']] = {
							'url_mapping': url_mapping,
							'full_url': f'{self.url}/services/apexrest{url_mapping}',
							'class_id': record.get('Id'),
						}

						# Try to call the endpoint
						try:
							endpoint_url = f'{self.url}/services/apexrest{url_mapping}'
							get_resp = self.session.get(endpoint_url, headers=auth_headers, timeout=15)
							results[record['Name']]['get_status'] = get_resp.status_code
							if get_resp.status_code == 200:
								try:
									results[record['Name']]['get_response'] = get_resp.json()
								except Exception:
									results[record['Name']]['get_response'] = get_resp.text[:1000]
								logger.warning(f'Apex REST endpoint responded: {url_mapping}')
						except Exception:
							pass
		except Exception:
			logger.debug('Error discovering Apex REST endpoints')
			logger.debug(traceback.format_exc())

		# Also try common known endpoint paths
		common_endpoints = ['/services/apexrest/', '/services/apexrest/api/', '/services/apexrest/v1/']
		for endpoint in common_endpoints:
			try:
				resp = self.session.get(
					f'{self.url}{endpoint}',
					headers=auth_headers, timeout=10
				)
				if resp.status_code == 200:
					results[f'probe:{endpoint}'] = {
						'url_mapping': endpoint,
						'full_url': f'{self.url}{endpoint}',
						'get_status': resp.status_code,
						'get_response': resp.text[:2000]
					}
					logger.verbose(f'Apex REST probe responded at {endpoint}')
			except Exception:
				pass

		if results:
			logger.warning(f'Discovered {len(results)} Apex REST endpoints')
		else:
			logger.info('No Apex REST endpoints discovered')

		return results


	def extract_via_bulk_api(self, objects, output_dir):
		"""Mass-extract data via Salesforce Bulk API"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping Bulk API extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		import time

		bulk_dir = os.path.join(output_dir, 'bulk_records')
		os.makedirs(bulk_dir, exist_ok=True)

		# Extract API version number from URL like /services/data/v66.0
		api_version = re.search(r'v(\d+\.\d+)', self.rest_api_url)
		if not api_version:
			logger.debug('Cannot determine API version for Bulk API')
			return results

		version = api_version.group(1)
		bulk_headers = {**auth_headers, 'Content-Type': 'application/json'}

		logger.info(f'Attempting Bulk API extraction for {len(objects)} objects')

		for object_name in objects:
			try:
				# Describe to get fields
				describe_resp = self.session.get(
					f'{self.url}{self.rest_api_url}/sobjects/{object_name}/describe',
					headers=auth_headers, timeout=30
				)
				if describe_resp.status_code != 200:
					continue

				describe = describe_resp.json()
				if not describe.get('queryable', False):
					continue

				fields = [f['name'] for f in describe.get('fields', []) if f.get('type') not in ('address', 'location')]
				if not fields:
					continue

				query = f"SELECT {','.join(fields)} FROM {object_name}"

				# Create Bulk 2.0 query job
				job_payload = {
					'operation': 'query',
					'query': query,
					'contentType': 'CSV',
					'columnDelimiter': 'COMMA',
					'lineEnding': 'LF'
				}
				create_resp = self.session.post(
					f'{self.url}/services/data/v{version}/jobs/query',
					headers=bulk_headers, json=job_payload, timeout=30
				)
				if create_resp.status_code not in (200, 201):
					logger.debug(f'Bulk job creation failed for {object_name}: HTTP {create_resp.status_code}')
					continue

				job_id = create_resp.json().get('id')
				if not job_id:
					continue

				# Poll for completion (max 2 minutes per object)
				max_wait = 120
				waited = 0
				state = 'UploadComplete'
				while state not in ('JobComplete', 'Aborted', 'Failed') and waited < max_wait:
					time.sleep(3)
					waited += 3
					status_resp = self.session.get(
						f'{self.url}/services/data/v{version}/jobs/query/{job_id}',
						headers=auth_headers, timeout=15
					)
					if status_resp.status_code == 200:
						state = status_resp.json().get('state', 'Failed')

				if state != 'JobComplete':
					logger.debug(f'Bulk job for {object_name} did not complete (state={state})')
					continue

				# Download results
				result_resp = self.session.get(
					f'{self.url}/services/data/v{version}/jobs/query/{job_id}/results',
					headers=auth_headers, timeout=120
				)
				if result_resp.status_code == 200 and len(result_resp.content) > 0:
					filepath = os.path.join(bulk_dir, f'{object_name}.csv')
					with open(filepath, 'w', encoding='utf-8') as f:
						f.write(result_resp.text)
					row_count = result_resp.text.count('\n') - 1
					results[object_name] = {'file': filepath, 'rows': max(row_count, 0)}
					logger.verbose(f'Bulk API: exported {row_count} rows for {object_name}')
			except Exception:
				logger.debug(f'Error during Bulk API extraction for {object_name}')
				logger.debug(traceback.format_exc())

		if results:
			logger.warning(f'Bulk API: exported {len(results)} objects to {bulk_dir}')
		else:
			logger.info('Bulk API: no data exported')

		return results


	def download_legacy_documents(self, all_records_sources, output_dir):
		"""Download legacy Document object files"""
		document_ids = set()

		for source in all_records_sources:
			if 'Document' in source:
				for record in source['Document'].get('records', []):
					if isinstance(record, dict):
						rid = record.get('Id') or record.get('id')
						if rid:
							document_ids.add(rid)

		# Also try to query Document table via REST
		if self.rest_api_accessible and self.rest_api_url:
			auth_headers = self._rest_headers()
			if auth_headers:
				try:
					resp = self.session.get(
						f'{self.url}{self.rest_api_url}/query?q={quote("SELECT Id,Name,ContentType,BodyLength FROM Document LIMIT 2000")}',
						headers=auth_headers, timeout=60
					)
					if resp.status_code == 200:
						records = resp.json().get('records', [])
						for r in records:
							if r.get('Id'):
								document_ids.add(r['Id'])
				except Exception:
					logger.debug('Error querying Document objects')

		if not document_ids:
			logger.info('No Document records found to download')
			return []

		logger.info(f'Downloading {len(document_ids)} legacy Documents')
		doc_dir = os.path.join(output_dir, 'documents')
		os.makedirs(doc_dir, exist_ok=True)

		downloaded = []
		for doc_id in document_ids:
			# REST blob
			if self.rest_api_accessible and self.rest_api_url:
				auth_headers = self._rest_headers()
				if auth_headers:
					try:
						resp = self.session.get(
							f'{self.url}{self.rest_api_url}/sobjects/Document/{doc_id}/Body',
							headers=auth_headers, timeout=120, stream=True
						)
						if resp.status_code == 200:
							content_disp = resp.headers.get('Content-Disposition', '')
							filename = doc_id
							m = re.search(r'filename[*]?=["\']?([^"\';\r\n]+)', content_disp)
							if m:
								safe = re.sub(r'[^\w.\-]', '_', m.group(1).strip())
								filename = f'{doc_id}_{safe}'

							filepath = os.path.join(doc_dir, filename)
							size = 0
							with open(filepath, 'wb') as f:
								for chunk in resp.iter_content(chunk_size=8192):
									f.write(chunk)
									size += len(chunk)
							downloaded.append({'id': doc_id, 'filename': filename, 'size': size})
							logger.verbose(f'Downloaded Document: {filename} ({size} bytes)')
							continue
					except Exception:
						pass

			# Legacy servlet fallback
			try:
				url = f'{self.url}/servlet/servlet.FileDownload?file={doc_id}'
				resp = self.session.get(url, headers=self.headers, allow_redirects=True, timeout=120, stream=True)
				if resp.status_code == 200 and int(resp.headers.get('Content-Length', '0')) > 0:
					filepath = os.path.join(doc_dir, doc_id)
					size = 0
					with open(filepath, 'wb') as f:
						for chunk in resp.iter_content(chunk_size=8192):
							f.write(chunk)
							size += len(chunk)
					downloaded.append({'id': doc_id, 'filename': doc_id, 'size': size})
					logger.verbose(f'Downloaded Document: {doc_id} ({size} bytes)')
			except Exception:
				logger.debug(f'Error downloading Document {doc_id}')

		if downloaded:
			logger.warning(f'Downloaded {len(downloaded)} Documents to {doc_dir}')
			manifest = os.path.join(doc_dir, 'manifest.json')
			with open(manifest, 'w') as f:
				json.dump(downloaded, f, indent=2)
		else:
			logger.info('No Documents could be downloaded')

		return downloaded


	def extract_deleted_records(self, objects):
		"""Extract deleted/archived records via queryAll -- recovers data the org thought was gone"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping deleted records extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		logger.info(f'Searching for deleted/archived records across {len(objects)} objects')

		for object_name in objects:
			try:
				describe_resp = self.session.get(
					f'{self.url}{self.rest_api_url}/sobjects/{object_name}/describe',
					headers=auth_headers, timeout=30
				)
				if describe_resp.status_code != 200:
					continue

				describe = describe_resp.json()
				if not describe.get('queryable', False):
					continue

				fields = [
					f['name'] for f in describe.get('fields', [])
					if f.get('type') not in ('address', 'location')
				]
				if not fields or 'IsDeleted' not in fields:
					continue

				query = f"SELECT {','.join(fields)} FROM {object_name} WHERE IsDeleted = true ALL ROWS"
				all_records = []
				query_url = f"{self.url}{self.rest_api_url}/queryAll?q={quote(query)}"

				while query_url:
					try:
						resp = self.session.get(query_url, headers=auth_headers, timeout=60)
						if resp.status_code != 200:
							break
						data = resp.json()
						records = data.get('records', [])
						for r in records:
							r.pop('attributes', None)
						all_records.extend(records)
						next_url = data.get('nextRecordsUrl')
						query_url = f"{self.url}{next_url}" if next_url else None
					except Exception:
						break

				if all_records:
					results[object_name] = {'records': all_records, 'total_count': len(all_records)}
					logger.warning(f'Recovered {len(all_records)} deleted records for {object_name}')
			except Exception:
				logger.debug(f'Error querying deleted records for {object_name}')

		if results:
			logger.warning(f'Recovered deleted records from {len(results)} objects')
		else:
			logger.info('No deleted records found')

		return results


	def extract_record_counts(self):
		"""Fast record count enumeration via REST limits/recordCount"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/limits/recordCount',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				data = resp.json()
				for obj in data.get('spikeRemovals', data.get('sobjects', data.get('sObjects', []))):
					if isinstance(obj, dict):
						results[obj.get('name', '')] = obj.get('count', 0)
				if not results and isinstance(data, dict):
					results = data
				logger.info(f'Record count enumeration: {len(results)} objects with data')
		except Exception:
			logger.debug('Error fetching record counts')

		return results


	def extract_connect_api_data(self):
		"""Extract data via Connect REST API: Files, Topics, Communities, CMS content"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping Connect API extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		connect_endpoints = [
			('files', '/connect/files/users/me'),
			('topics', '/connect/topics'),
			('communities', '/connect/communities'),
			('managed_content', '/connect/managed-content/delivery'),
			('organization', '/connect/organization'),
			('user_profiles', '/connect/user-profiles/me'),
		]

		for name, endpoint in connect_endpoints:
			try:
				resp = self.session.get(
					f'{self.url}{self.rest_api_url}{endpoint}',
					headers=auth_headers, timeout=30
				)
				if resp.status_code == 200:
					data = resp.json()
					if data:
						results[name] = data
						logger.verbose(f'Connect API: retrieved {name} data')
			except Exception:
				logger.debug(f'Error fetching Connect API {name}')

		# Files: get all files shared with user
		if 'files' in results:
			try:
				files_data = results['files']
				file_items = files_data.get('files', files_data.get('elements', []))
				if isinstance(file_items, list):
					logger.verbose(f'Connect API: found {len(file_items)} files')
			except Exception:
				pass

		if results:
			logger.warning(f'Connect API: extracted data from {len(results)} endpoints')
		else:
			logger.info('No Connect API data accessible')

		return results


	def extract_wave_analytics(self):
		"""Extract CRM Analytics (Tableau CRM / Wave) datasets and lenses"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			logger.info('REST API not available, skipping Wave/Analytics extraction')
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		# List datasets
		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/wave/datasets',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				data = resp.json()
				datasets = data.get('datasets', [])
				logger.info(f'Wave: found {len(datasets)} datasets')

				for ds in datasets:
					ds_id = ds.get('id')
					ds_name = ds.get('name', ds_id)
					version_url = ds.get('currentVersionUrl')

					if version_url:
						try:
							ver_resp = self.session.get(
								f'{self.url}{version_url}',
								headers=auth_headers, timeout=30
							)
							if ver_resp.status_code == 200:
								version_data = ver_resp.json()
								dataset_url = version_data.get('dataset', {}).get('url', '')
								# Query the dataset
								query_payload = {
									'query': f'q = load "{ds_id}/{version_data.get("id", "")}"; q = foreach q generate all;'
								}
								query_resp = self.session.post(
									f'{self.url}{self.rest_api_url}/wave/query',
									headers={**auth_headers, 'Content-Type': 'application/json'},
									json=query_payload, timeout=60
								)
								if query_resp.status_code == 200:
									results[ds_name] = query_resp.json()
									logger.verbose(f'Wave: extracted dataset {ds_name}')
						except Exception:
							logger.debug(f'Error extracting Wave dataset {ds_name}')

				if not datasets:
					results['_datasets_meta'] = data
			elif resp.status_code == 404:
				logger.info('Wave/Analytics API not available on this org')
			else:
				logger.debug(f'Wave API returned HTTP {resp.status_code}')
		except Exception:
			logger.debug('Error accessing Wave API')

		# List lenses
		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/wave/lenses',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				lenses = resp.json().get('lenses', [])
				if lenses:
					results['_lenses'] = lenses
					logger.verbose(f'Wave: found {len(lenses)} lenses')
		except Exception:
			logger.debug('Error listing Wave lenses')

		# List dashboards (Wave dashboards, different from standard)
		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/wave/dashboards',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				wave_dashboards = resp.json().get('dashboards', [])
				if wave_dashboards:
					results['_wave_dashboards'] = wave_dashboards
					logger.verbose(f'Wave: found {len(wave_dashboards)} analytics dashboards')
		except Exception:
			logger.debug('Error listing Wave dashboards')

		if results:
			logger.warning(f'Wave/Analytics: extracted {len(results)} items')
		else:
			logger.info('No Wave/Analytics data accessible')

		return results


	def extract_ui_api_records(self, record_ids_sample):
		"""Extract records via the UI API -- parallel data path to Aura's RecordUiController"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		if not record_ids_sample:
			return results

		# UI API can fetch up to 200 records in one call
		batch_size = 200
		all_ids = list(record_ids_sample)[:500]

		logger.info(f'Fetching {len(all_ids)} records via UI API')

		for i in range(0, len(all_ids), batch_size):
			batch = all_ids[i:i+batch_size]
			ids_param = ','.join(batch)
			try:
				resp = self.session.get(
					f'{self.url}{self.rest_api_url}/ui-api/records/batch/{ids_param}',
					headers=auth_headers, timeout=60
				)
				if resp.status_code == 200:
					data = resp.json()
					batch_results = data.get('results', [])
					for item in batch_results:
						if item.get('statusCode') == 200 and item.get('result'):
							record = item['result']
							record_id = record.get('id', '')
							results[record_id] = record
							logger.verbose(f'UI API: retrieved record {record_id}')
			except Exception:
				logger.debug(f'Error fetching UI API batch at offset {i}')

		if results:
			logger.info(f'UI API: retrieved {len(results)} records')

		return results


	def extract_process_data(self):
		"""Extract approval processes and workflow rules"""
		results = {}

		if not self.rest_api_accessible or not self.rest_api_url:
			return results

		auth_headers = self._rest_headers()
		if not auth_headers:
			return results

		# Approval processes
		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/process/approvals',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				data = resp.json()
				results['approvals'] = data
				approvals = data.get('approvals', {})
				logger.verbose(f'Process: found approval processes for {len(approvals)} objects')
		except Exception:
			logger.debug('Error fetching approval processes')

		# Workflow rules
		try:
			resp = self.session.get(
				f'{self.url}{self.rest_api_url}/process/rules',
				headers=auth_headers, timeout=30
			)
			if resp.status_code == 200:
				data = resp.json()
				results['rules'] = data
				rules = data.get('rules', {})
				logger.verbose(f'Process: found workflow rules for {len(rules)} objects')
		except Exception:
			logger.debug('Error fetching workflow rules')

		if results:
			logger.warning(f'Process data: extracted approvals and rules')
		else:
			logger.info('No process data accessible')

		return results
