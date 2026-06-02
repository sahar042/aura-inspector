# aura-inspector

This is not an officially supported Google product. This project is not eligible for the [Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security).


## Introduction

<b>aura-inspector</b> is a Swiss Army knife of Salesforce Experience Cloud testing. It facilitates in discovering misconfigured Salesforce Experience Cloud applications as well as automates much of the testing process. For more information, please refer to the Mandiant blog post: [Auditing Salesforce Aura Data Exposure](https://cloud.google.com/blog/topics/threat-intelligence/auditing-salesforce-aura-data-exposure).

Some of it's functionality includes:
- Discovery and extraction of accessible records from both Guest and Authenticated contexts
- Full data extraction via Aura, GraphQL, REST API (SOQL), Bulk API, and Tooling API
- File downloads (ContentVersion, ContentDocument, Static Resources, legacy Attachments and Documents)
- Apex source code extraction (classes, triggers, Visualforce pages, Lightning components)
- Related record traversal by following lookup/relationship field IDs
- Search across all objects via SOSL and Aura search actions
- Chatter/feed extraction, Knowledge articles, Reports, Dashboards, CRM Analytics (Wave)
- Custom Apex controller invocation and Apex REST endpoint discovery
- Custom Settings, Custom Metadata Types, and full object schema extraction
- Deleted/archived record recovery via queryAll
- Checks for self-registration, REST API, SOAP API, and GraphQL availability
- Discovery of Record List components and object Home URLs

## Installation

### pipx (Recommended)

The tool can be installed with pipx using the command below.
```
pipx install git+<URL>
```

### pip

The tool requires Python 3 to run and pip to download the dependencies. We recommend creating a virtual environment to install the dependencies.
```
git clone <URL>
cd aura-inspector
virtualenv env
source ./env/bin/activate
pip3 install -r requirements.txt
```

## Getting Started
The help menu can be invoked using the <b>-h</b> flag, which provides a list of configurations.

```
python3 aura_cli.py -h
usage: python3 aura_cli.py [-h] [-u URL] [-c COOKIES] [-o OUTPUT_DIR]
                           [-l OBJECT_LIST] [-d] [-v] [-p PROXY] [-k]
                           [--app APP] [--aura AURA] [--context CONTEXT]
                           [--token TOKEN] [--no-gql] [--fetch-all]
                           [--no-banner] [--no-deep] [--no-files]
                           [--search-terms SEARCH_TERMS]
                           [-r AURA_REQUEST_FILE]

options:
  -h, --help            show this help message and exit
  -u, --url URL         Root URL of Salesforce application to audit
  -c, --cookies COOKIES
                        Cookies after authenticating to Salesforce application
  -o, --output-dir OUTPUT_DIR
                        Output directory
  -l, --object-list OBJECT_LIST
                        Pull data of only the provided objects. Comma
                        separated list of objects.
  -d, --debug           Print debug information
  -v, --verbose         Print verbose information
  -p, --proxy PROXY     Proxy requests
  -k, --insecure        Ignore invalid TLS certificates
  --app APP             Provide the target salesforce app's path (e.g:
                        /myApp), the script will try to detect it if not
                        provided
  --aura AURA           Provide the target salesforce aura's path (e.g:
                        /aura), the script will try to detect it if not
                        provided
  --context CONTEXT     Provide a context to be used as aura.context in POST
                        requests, the script will use a dummy one if not
                        provided
  --token TOKEN         Provide an aura token to be used as aura.token in POST
                        requests, the script will use a dummy one if not
                        provided
  --no-gql              Do not check for GraphQL capability and do not use it
  --fetch-all           Fetch all records using pagination instead of only the
                        first page
  --no-banner           Do not display banner
  --no-deep             Skip deep extraction (REST SOQL, search, related
                        records, chatter, reports, dashboards, custom
                        controller invocation)
  --no-files            Skip downloading ContentVersion/ContentDocument file
                        blobs
  --search-terms SEARCH_TERMS
                        Comma-separated search terms for SOSL/Aura search
                        (default: common sensitive keywords)
  -r, --aura-request-file AURA_REQUEST_FILE
                        Provide a request file to an /aura endpoint
```

The tool offers a variety of options that can be useful in different scenarios. The following cover a few different situations.

## Basic Usage

Using the tool in the standard configuration is as simple as running the following command. This will run all checks in an unauthenticated manner and return what is accessible from a Guest user perspective.

`python3 aura_cli.py -u <URL>`

The output will also reveal whether there is a self-registration functionality you can use to create an account. If you do have the opportunity to signup on the instance, running the tool from an authenticated context will likely yield more results. 

To run the tool in an authenticated context, either supply the SID cookie using the <b>-c</b> parameter or let the tool parse this and other parameters for you by supplying a file with the contents of an arbitrary request to the aura endpoint in an authenticated session. 

`python3 aura_cli.py -r <AURA_REQUEST_FILE>`

## Extracting Record Data

This is a modified version of the original [google/aura-inspector](https://github.com/google/aura-inspector). The original tool only counts records per object but never extracts the actual data. This fork adds full record extraction and comprehensive data exfiltration testing across all Salesforce API surfaces.

To extract all accessible record data and save it to a directory:

`python3 aura_cli.py -u <URL> -o ./results`

By default, the tool fetches only the first page of records (100 for Aura, 50 for GraphQL). To fetch **all** records using pagination:

`python3 aura_cli.py -u <URL> -o ./results --fetch-all`

By default, the tool fetches only the first page of records (100 for Aura, 50 for GraphQL). To fetch **all** records using pagination:

`python3 aura_cli.py -u <URL> -o ./results --fetch-all`

You can also target specific objects:

`python3 aura_cli.py -u <URL> -l User,ContentDocument,CspTrustedSite -o ./results`

## Deep Extraction

By default, the tool performs deep extraction beyond basic Aura/GraphQL record enumeration, covering the full Salesforce API attack surface. Features are grouped by which context they work in:

### Guest User (unauthenticated)
- **Aura `getItems` record enumeration** - Standard record extraction via `SelectableListDataProviderController`
- **GraphQL record extraction** - Record enumeration with field-level data via `executeGraphQL`
- **Custom Apex controller invocation** - Calls discovered `apex://` controllers with empty params to check for data leaks
- **Related record traversal** - Follows relationship/lookup field IDs to fetch records not found via list enumeration
- **Aura search** - Searches for sensitive keywords via `SearchController`
- **ContentVersion/ContentDocument file downloads** - Downloads actual file blobs via `/sfc/servlet.shepherd/`
- **Aura component definitions** - Extracts component source to reveal custom logic
- **Knowledge articles (Aura)** - KB article extraction via Aura actions
- **Chatter feeds (Aura)** - Feed extraction via Aura actions

### Authenticated (requires `-c` SID cookie)
- **REST API SOQL extraction** - Full `SELECT *` SOQL queries against all objects with pagination
- **Deleted/archived record recovery** - Uses `queryAll` to recover records the org thought were deleted
- **Tooling API** - Extracts Apex classes, triggers, Visualforce pages, Lightning component source code
- **Bulk API 2.0** - Mass CSV export of entire object datasets
- **Static Resource downloads** - Downloads JS, CSS, ZIP bundles (may contain API keys/secrets)
- **Legacy Attachment and Document downloads** - Old-style file objects via REST blob endpoint or servlet
- **SOSL search** - Keyword search across all objects via REST
- **Metadata describe** - Full object/field schema (field names, types, relationships, picklist values)
- **Custom Settings / Custom Metadata Types** - Org-wide configuration data
- **Apex REST endpoint discovery** - Finds `@RestResource` annotated classes and probes the endpoints
- **Reports & Dashboards** - Discovers and extracts accessible report/dashboard data
- **CRM Analytics (Wave)** - Tableau CRM datasets, lenses, and analytics dashboards
- **Connect API** - Files, Topics, Communities, CMS content
- **UI API** - Record views via `/ui-api/records/batch`
- **Chatter (REST)** - Company/news feeds, groups via REST Chatter API
- **Knowledge articles (REST)** - KB articles via REST + SOQL on `__kav` objects
- **Process data** - Approval processes and workflow rules
- **Record count enumeration** - Fast recon via `/limits/recordCount`

### Controlling Deep Extraction

To skip all deep extraction features:

`python3 aura_cli.py -u <URL> --no-deep`

To skip only file/binary downloads:

`python3 aura_cli.py -u <URL> --no-files`

To use custom search terms:

`python3 aura_cli.py -u <URL> --search-terms "ssn,credit card,password,api key"`

## Output Directory Structure

```
results/
├── records/              # Aura getItems records (JSON per object)
├── gql_records/          # GraphQL records (JSON per object)
├── rest_records/         # REST API SOQL records (JSON per object)
├── deleted_records/      # Recovered deleted/archived records
├── bulk_records/         # Bulk API 2.0 CSV exports
├── source_code/          # Tooling API: Apex/VF/Lightning source
│   ├── ApexClass/
│   ├── ApexTrigger/
│   ├── ApexPage/
│   ├── ApexComponent/
│   ├── AuraDefinitionBundle/
│   └── AuraDefinition/
├── files/                # ContentVersion/ContentDocument file blobs
│   └── manifest.json
├── static_resources/     # Static Resource file downloads
│   └── manifest.json
├── attachments/          # Legacy Attachment file downloads
│   └── manifest.json
├── documents/            # Legacy Document file downloads
│   └── manifest.json
└── misc/
    ├── csp_trusted_sites.json
    ├── recordlists.json
    ├── homeurls.json
    ├── custom_controllers.json
    ├── custom_controller_data.json
    ├── related_records.json
    ├── search_results.json
    ├── chatter_feeds.json
    ├── reports.json
    ├── dashboards.json
    ├── metadata_describe.json
    ├── knowledge_articles.json
    ├── custom_settings.json
    ├── aura_component_defs.json
    ├── apex_rest_endpoints.json
    ├── record_counts.json
    ├── connect_api.json
    ├── wave_analytics.json
    ├── ui_api_records.json
    └── process_data.json
```

## Handling Multiple Apps

A single instance could have multiple custom apps hosted on it. This could typically be identified if you see something along the lines of `/<custom-app-name>/s` in the path. If this is the case, we recommend finding all apps, and specifying them using the `--app` parameter, as the output could differ significantly. It's also advised to try run the tool against the default app "/" if there are any custom apps hosted on the instance.

# Developed By:
- Amine Ismail
- Anirudha Kanodia

# Modified By:
- Sahar Shlichove
