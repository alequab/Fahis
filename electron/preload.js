// The only thing the page can reach: one request function, with no access to Node.js or the files.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("fahis", {
  request: (kind, payload) => ipcRenderer.invoke("fahis:request", kind, payload),
});
