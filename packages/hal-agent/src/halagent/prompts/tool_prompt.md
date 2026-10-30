## Tools

To call a tool, write exactly one block like this and nothing after it:
<tool_call>
{"name": "TOOL_NAME", "arguments": {"ARG": "VALUE"}}
</tool_call>
The result comes back in the next message. Your tools:
{tool_lines}
