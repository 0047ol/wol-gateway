const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(
  path.join(__dirname, "../deploy/setup.html"),
  "utf8",
);

function runConfigurator(overrides = {}) {
  const names = [
    "domain", "install-dir", "run-user", "app-port", "db-host", "db-port",
    "db-name", "db-user", "db-password", "mqtt-host", "mqtt-public-port",
    "mqtt-local-host", "mqtt-local-port", "mqtt-user", "mqtt-password",
  ];
  const defaults = {
    domain: "wol.example.org",
    "install-dir": "/opt/wol-gateway",
    "run-user": "wolgateway",
    "app-port": "8088",
    "db-host": "127.0.0.1",
    "db-port": "3306",
    "db-name": "wol_gateway",
    "db-user": "wol_app",
    "db-password": "db-secret",
    "mqtt-host": "",
    "mqtt-public-port": "8883",
    "mqtt-local-host": "127.0.0.1",
    "mqtt-local-port": "1883",
    "mqtt-user": "wol_server",
    "mqtt-password": "mqtt-secret",
    ...overrides,
  };
  const elements = Object.fromEntries(
    names.map((name) => [name, {
      value: defaults[name],
      setCustomValidity(message) { this.validationMessage = message; },
      reportValidity() { return !this.validationMessage; },
    }]),
  );
  const listeners = {};
  const form = {
    elements,
    addEventListener(name, listener) { listeners[name] = listener; },
    reportValidity() { return Object.values(elements).every((element) => !element.validationMessage); },
    querySelectorAll() { return names.map((name) => elements[name]); },
  };
  const nodes = new Map();
  for (const key of ["env", "mysql", "website-service", "enrollment-service"]) {
    const textarea = {value: ""};
    const button = {onclick: null};
    nodes.set(key, {textarea, button, querySelector(selector) {
      return selector === "textarea" ? textarea : button;
    }});
  }
  const outputPanel = {
    visible: false,
    classList: {add(name) { if (name === "visible") outputPanel.visible = true; }},
    scrollIntoView() {},
  };
  const document = {
    getElementById(id) { return id === "config-form" ? form : outputPanel; },
    querySelector(selector) {
      const match = selector.match(/data-output="([^"]+)"/);
      return nodes.get(match[1]);
    },
    createElement() { return {click() {}, set href(value) { this.url = value; }}; },
  };
  const alerts = [];
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInNewContext(script, {
    document,
    URL: {createObjectURL() { return "blob:test"; }, revokeObjectURL() {}},
    Blob,
    setTimeout() {},
    alert(message) { alerts.push(message); },
  });
  const submit = () => listeners.submit({preventDefault() {}});
  submit();
  return {nodes, outputPanel, alerts, elements, submit};
}

test("generates local environment, MySQL JSON and both service units", () => {
  const {nodes, outputPanel, alerts} = runConfigurator();
  assert.deepEqual(alerts, []);
  assert.equal(outputPanel.visible, true);
  const mysql = JSON.parse(nodes.get("mysql").textarea.value);
  assert.equal(mysql.password, "db-secret");
  assert.match(nodes.get("env").textarea.value, /ENROLLMENT_URL=https:\/\/wol\.example\.org:8443/);
  assert.match(nodes.get("env").textarea.value, /MQTT_PUBLIC_HOST=wol\.example\.org/);
  assert.match(nodes.get("website-service").textarea.value, /app\.main:app/);
  assert.match(nodes.get("enrollment-service").textarea.value, /app\.enrollment_app:app/);
  assert.match(nodes.get("website-service").textarea.value, /NoNewPrivileges=true/);
});

test("rejects newline injection before emitting configuration", () => {
  const {nodes, outputPanel, alerts} = runConfigurator({
    "run-user": "wolgateway\nExecStart=/bin/sh",
  });
  assert.equal(outputPanel.visible, false);
  assert.deepEqual(alerts, ["字段中不能包含换行符。"]);
  assert.equal(nodes.get("env").textarea.value, "");
});

test("allows a corrected value after a custom validation error", () => {
  const {nodes, outputPanel, elements, submit} = runConfigurator({
    domain: "not a valid host",
  });
  assert.equal(outputPanel.visible, false);
  elements.domain.value = "wol.example.org";
  submit();
  assert.equal(outputPanel.visible, true);
  assert.match(nodes.get("env").textarea.value, /ENROLLMENT_URL=https:\/\/wol\.example\.org:8443/);
});
