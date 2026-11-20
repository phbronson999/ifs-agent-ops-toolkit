import mcp.types as mcp_types


projection_definition = mcp_types.Prompt(
    name="build_ifs_skill_from_projection",
    description="Build an IFS skill by fetching the OpenAPI spec live for a named projection.",
    arguments=[
        mcp_types.PromptArgument(
            name="projection_name",
            description="IFS projection service name, e.g. 'CustomerHandling', 'TechPortalTaskExecution'.",
            required=True,
        ),
        mcp_types.PromptArgument(
            name="skill_name",
            description="Filename for the skill (without .md), e.g. 'ifs-parts', 'ifs-sales-customers'.",
            required=True,
        ),
    ],
)


async def projection_handler(args: dict[str, str]) -> mcp_types.GetPromptResult:
    projection_name = args.get("projection_name", "")
    skill_name = args.get("skill_name", "")
    spec_endpoint = f"/main/ifsapplications/projection/v1/{projection_name}.svc/$openapi?V2"
    return mcp_types.GetPromptResult(
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(
                    type="text",
                    text=(
                        f"I want to build an IFS resource guide for the {projection_name} projection. "
                        f"The OpenAPI spec is at {spec_endpoint}. "
                        f"Save the skill as {skill_name}. "
                        "Please load the ifs-skill-authoring guide via get_api_guide, "
                        "then fetch the spec and guide me through building the skill."
                    ),
                ),
            )
        ]
    )


har_definition = mcp_types.Prompt(
    name="build_ifs_skill_from_har",
    description="Build an IFS skill from a browser HAR recording.",
    arguments=[
        mcp_types.PromptArgument(
            name="har_file_path",
            description="Absolute path to a .har file exported from browser DevTools.",
            required=True,
        ),
        mcp_types.PromptArgument(
            name="skill_name",
            description="Filename for the skill (without .md), e.g. 'ifs-purchase-orders'.",
            required=True,
        ),
    ],
)


async def har_handler(args: dict[str, str]) -> mcp_types.GetPromptResult:
    har_file_path = args.get("har_file_path", "")
    skill_name = args.get("skill_name", "")
    return mcp_types.GetPromptResult(
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(
                    type="text",
                    text=(
                        "I want to build an IFS resource guide from a HAR file. "
                        f"Save the skill as {skill_name}. "
                        f"Please call parse_har_file with path {har_file_path} to analyse it, "
                        "load the ifs-skill-authoring guide via get_api_guide, "
                        "then guide me through building the skill."
                    ),
                ),
            )
        ]
    )


openapi_definition = mcp_types.Prompt(
    name="build_ifs_skill_from_openapi",
    description="Build an IFS skill from a downloaded OpenAPI/Swagger spec file.",
    arguments=[
        mcp_types.PromptArgument(
            name="openapi_file_path",
            description="Absolute path to a downloaded OpenAPI/Swagger JSON file.",
            required=True,
        ),
        mcp_types.PromptArgument(
            name="skill_name",
            description="Filename for the skill (without .md), e.g. 'ifs-parts'.",
            required=True,
        ),
    ],
)


async def openapi_handler(args: dict[str, str]) -> mcp_types.GetPromptResult:
    openapi_file_path = args.get("openapi_file_path", "")
    skill_name = args.get("skill_name", "")
    return mcp_types.GetPromptResult(
        messages=[
            mcp_types.PromptMessage(
                role="user",
                content=mcp_types.TextContent(
                    type="text",
                    text=(
                        "I want to build an IFS resource guide from an OpenAPI spec. "
                        f"Save the skill as {skill_name}. "
                        f"Please call read_openapi_file with path {openapi_file_path} to analyse it, "
                        "load the ifs-skill-authoring guide via get_api_guide, "
                        "then guide me through building the skill."
                    ),
                ),
            )
        ]
    )
