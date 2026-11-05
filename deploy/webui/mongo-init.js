// One-time setup when the webui-mongo volume is empty.
const p = process.env.MONGO_ACS_PASSWORD;
if (!p || !/^[a-f0-9]{32,}$/i.test(p)) {
  throw new Error("MONGO_ACS_PASSWORD must be a random hex string (32+ characters)");
}
db.getSiblingDB("genieacs").createUser({
  user: "acs",
  pwd: p,
  roles: [{ role: "readWrite", db: "genieacs" }]
});

// Do not accept unauthenticated devices from the public Internet.
// Enroll only devices configured with these values, later migrate to
// per-device cwmp.auth credentials for production fleets.
const u = process.env.CPE_USERNAME;
const pw = process.env.CPE_PASSWORD;
if (!u || !pw) throw new Error("CPE_USERNAME and CPE_PASSWORD required");
db.getSiblingDB("genieacs").config.updateOne(
  { _id: "cwmp.auth" },
  { $set: { value: `AUTH(${JSON.stringify(u)}, ${JSON.stringify(pw)})` } },
  { upsert: true }
);
