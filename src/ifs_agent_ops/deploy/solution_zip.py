"""Build an unmanaged solution file (zip) that carries exactly the components to create or update.

Importing a solution is how connectors are deployed: a connector created with ``pac connector create`` never
gets the on-premises data gateway (pac drops the capability) and is named ``new_...``; one imported inside a
solution keeps the gateway set at creation and gets the publisher's prefix. Importing an unmanaged solution
adds and updates; it never removes components.
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

CONNECTOR_COMPONENT = 372
ENVIRONMENT_VARIABLE_COMPONENT = 380
GATEWAY_CAPABILITY = "[-1,118690003,-1]"   # the connector record's value for "Connect via on-premises data gateway"
VARIABLE_TYPE_CODES = {"string": 100000000, "number": 100000001, "boolean": 100000002, "json": 100000003}
_CONTENT_TYPES = ('<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/'
                  '2006/content-types"><Default Extension="xml" ContentType="application/octet-stream" />'
                  '<Default Extension="json" ContentType="application/octet-stream" /></Types>')


@dataclass
class Publisher:
    unique_name: str
    friendly_name: str
    prefix: str
    option_value_prefix: int


@dataclass
class ConnectorPart:
    connector_id: str
    name: str                 # schema name, e.g. ti_ifscustomerorderhandling
    display_name: str
    description: str
    definition: dict          # apiDefinition with host and title already set
    connection_parameters: dict
    icon_brand_color: str = "#007ee5"


@dataclass
class VariablePart:
    schema_name: str
    display_name: str
    description: str
    type: str
    value: str | None = None


@dataclass
class ReferencePart:
    logical_name: str
    display_name: str
    connector_api_id: str     # /providers/Microsoft.PowerApps/apis/<connector internal id>
    custom_connector_id: str = ""   # the connector record's id; without it the solution cannot be exported


@dataclass
class SolutionFile:
    unique_name: str
    display_name: str
    version: str
    publisher: Publisher
    connectors: list[ConnectorPart] = field(default_factory=list)
    variables: list[VariablePart] = field(default_factory=list)
    references: list[ReferencePart] = field(default_factory=list)


def _label(tag: str, text: str) -> str:
    return (f'<{tag} default={quoteattr(text)}><label description={quoteattr(text)} languagecode="1033" />'
            f"</{tag}>")


def solution_xml(sol: SolutionFile) -> str:
    pub = sol.publisher
    roots = [f'<RootComponent type="{CONNECTOR_COMPONENT}" id="{{{c.connector_id}}}" schemaName={quoteattr(c.name)} '
             f'behavior="0" />' for c in sol.connectors]
    roots += [f'<RootComponent type="{ENVIRONMENT_VARIABLE_COMPONENT}" schemaName={quoteattr(v.schema_name)} '
              f'behavior="0" />' for v in sol.variables]
    return f"""<ImportExportXml version="9.2" SolutionPackageVersion="9.2" languagecode="1033" generatedBy="ifs-agent-ops" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <SolutionManifest>
    <UniqueName>{escape(sol.unique_name)}</UniqueName>
    <LocalizedNames><LocalizedName description={quoteattr(sol.display_name)} languagecode="1033" /></LocalizedNames>
    <Descriptions />
    <Version>{escape(sol.version)}</Version>
    <Managed>0</Managed>
    <Publisher>
      <UniqueName>{escape(pub.unique_name)}</UniqueName>
      <LocalizedNames><LocalizedName description={quoteattr(pub.friendly_name)} languagecode="1033" /></LocalizedNames>
      <Descriptions />
      <CustomizationPrefix>{escape(pub.prefix)}</CustomizationPrefix>
      <CustomizationOptionValuePrefix>{pub.option_value_prefix}</CustomizationOptionValuePrefix>
    </Publisher>
    <RootComponents>
      {chr(10).join("      " + r for r in roots).strip()}
    </RootComponents>
    <MissingDependencies />
  </SolutionManifest>
</ImportExportXml>
"""


def customizations_xml(sol: SolutionFile) -> str:
    connectors = "".join(f"""
    <Connector>
      <connectorid>{c.connector_id}</connectorid>
      <description>{escape(c.description)}</description>
      <displayname>{escape(c.display_name)}</displayname>
      <iconbrandcolor>{escape(c.icon_brand_color)}</iconbrandcolor>
      <name>{escape(c.name)}</name>
      <connectortype>1</connectortype>
      <capabilities>{GATEWAY_CAPABILITY}</capabilities>
      <openapidefinition>/Connector/{c.name}_openapidefinition.json</openapidefinition>
      <connectionparameters>/Connector/{c.name}_connectionparameters.json</connectionparameters>
      <policytemplateinstances>/Connector/{c.name}_policytemplateinstances.json</policytemplateinstances>
    </Connector>""" for c in sol.connectors)
    references = "".join(f"""
    <connectionreference connectionreferencelogicalname={quoteattr(r.logical_name)}>
      <connectionreferencedisplayname>{escape(r.display_name)}</connectionreferencedisplayname>
      <connectorid>{escape(r.connector_api_id)}</connectorid>{_custom_connector(r)}
      <iscustomizable>1</iscustomizable>
      <promptingbehavior>0</promptingbehavior>
      <statecode>0</statecode>
      <statuscode>1</statuscode>
    </connectionreference>""" for r in sol.references)
    return f"""<ImportExportXml xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <Entities /><Roles /><Workflows /><FieldSecurityProfiles /><Templates /><EntityMaps /><EntityRelationships />
  <OrganizationSettings /><optionsets /><CustomControls /><EntityDataProviders />
  <Connectors>{connectors}
  </Connectors>
  <connectionreferences>{references}
  </connectionreferences>
  <Languages><Language>1033</Language></Languages>
</ImportExportXml>
"""


def _custom_connector(r: ReferencePart) -> str:
    if not r.custom_connector_id:
        return ""
    return (f"\n      <customconnectorid>\n        <connectorid>{escape(r.custom_connector_id)}</connectorid>"
            "\n      </customconnectorid>")


def variable_definition_xml(v: VariablePart) -> str:
    return (f"<environmentvariabledefinition schemaname={quoteattr(v.schema_name)}>"
            f"{_label('description', v.description)}{_label('displayname', v.display_name)}"
            "<iscustomizable>1</iscustomizable><isrequired>0</isrequired><secretstore>0</secretstore>"
            f"<type>{VARIABLE_TYPE_CODES[v.type]}</type></environmentvariabledefinition>")


def write_solution(sol: SolutionFile, path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("solution.xml", solution_xml(sol))
        z.writestr("customizations.xml", customizations_xml(sol))
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        for c in sol.connectors:
            z.writestr(f"Connector/{c.name}_openapidefinition.json", json.dumps(c.definition, separators=(",", ":")))
            z.writestr(f"Connector/{c.name}_connectionparameters.json", json.dumps(c.connection_parameters, indent=2))
            z.writestr(f"Connector/{c.name}_policytemplateinstances.json", "[]")
        for v in sol.variables:
            folder = f"environmentvariabledefinitions/{v.schema_name}"
            z.writestr(f"{folder}/environmentvariabledefinition.xml", variable_definition_xml(v))
            if v.value is not None:
                z.writestr(f"{folder}/environmentvariablevalues.json", json.dumps(
                    {"environmentvariablevalues": {"environmentvariablevalue": {"iscustomizable": "1",
                                                                                "value": v.value}}}, indent=2))
    return path
