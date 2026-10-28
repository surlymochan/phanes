Cut the renderer cost of long chats, big lists and the sidebar

The conversation snapshot compares messages field by field instead of with JSON.stringify, the timeline reuses its rows, long chats and big lists are virtualized, resize observers attach only while they animate, and the keyed stores no longer copy whole maps.
