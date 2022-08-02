# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import uuid

aa_client = boto3.client('accessanalyzer')
analyzer_arn = ""


# get or create access analyzer

try:
    if active_analyzers := [
        a
        for a in aa_client.list_analyzers(type="ACCOUNT").get("analyzers")
        if a["status"] == "ACTIVE"
    ]:
        # take the first active analyzer if there are any active analyzer
        analyzer_arn = active_analyzers[0]["arn"]
    else:
        # try to create a new analyzer if there is no analyzer already created for the account
        a_name = f"AccessAnalyzer-{str(uuid.uuid1())}"
        analyzer_arn = aa_client.create_analyzer(
            analyzerName=a_name,
            type="ACCOUNT").get("arn")

except Exception as e:
    print(f"Exception during get analyzer: {str(e)}")

# get all KMS keys in the account in the region
kms_client = boto3.client("kms")

customer_keys_arns = []
for page in kms_client.get_paginator("list_keys").paginate():
    # get the KeyManager (AWS or Customer) for each returned key
    for k in page["Keys"]:
        k_data = kms_client.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
        # take only Customer key (where KeyManager not AWS)
        if k_data["KeyManager"] not in "AWS":
            customer_keys_arns.append(k_data["Arn"])

# scan customer keys using access analyser
resource_scan = {}

# initiate scan for reources
for r_arn in customer_keys_arns:
    res = aa_client.start_resource_scan(
        analyzerArn=analyzer_arn,
        resourceArn=r_arn
        )
    print(f"Start_resouce_scan for {r_arn}:{res}")

    resource_scan[r_arn] = False

import time
import json

# wait till all resources get analyzed
MAX_LIST_ANALYZED_RESOURSES_ATTEMPTS = 10
rType = "AWS::KMS::Key"

for _ in range(MAX_LIST_ANALYZED_RESOURSES_ATTEMPTS):

    for page in aa_client.get_paginator("list_analyzed_resources").paginate(analyzerArn=analyzer_arn, resourceType=rType):
        for r in page["analyzedResources"]:
            if r["resourceArn"] in resource_scan:
                resource_scan[r["resourceArn"]] = True

    pending = {r:s for r,s in resource_scan.items() if not s}

    if not pending: # exit if all requested resources are processed
            break
    time.sleep(0.5)
else:
    print(f"Max number ({MAX_LIST_ANALYZED_RESOURSES_ATTEMPTS}) of attempts to call list_analyzed_resources reached")
    print(f"The following resources weren't analyzed: {json.dumps(pending, indent=2)}")

# get resource scan result only on analyzed resources
findings = []

for r_arn in {r for r,s in resource_scan.items() if s}:
    res = aa_client.get_analyzed_resource(
        analyzerArn=analyzer_arn,
        resourceArn=r_arn)

    resource = res["resource"]
    if resource.get("isPublic") and resource.get("status") in "ACTIVE":
        print(f"Found public resource: {r_arn}:{resource}")
        findings.append(resource)

# publish findings to the EventBridge default bus
events = boto3.client('events')

import json
import datetime

# class JSONEncoder
class DateTimeEncoder(json.JSONEncoder):
        #Override the default method
        def default(self, obj):
            if isinstance(obj, (datetime.date, datetime.datetime)):
                return str(obj.isoformat())

if bool(findings):
    r = events.put_events(
        Entries=[
            {
                "Source":"access-analyzer-kms-function",
                "Resources":[r["resourceArn"] for r in findings],
                "DetailType":"Access Analyzer KMS Findings",
                "Detail":json.dumps({"Findings":findings}, indent=2, cls=DateTimeEncoder), 
            }
        ]
    )