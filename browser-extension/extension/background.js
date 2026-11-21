// The toolbar button opens the capture side panel. All capture work happens in the side panel
// (sidepanel.js), which stays open for the whole recording.
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((e) => console.warn(e));
