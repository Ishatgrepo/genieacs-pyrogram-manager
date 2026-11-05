// Runs only when the MongoDB volume is empty. Use random hex passwords in .env.
for (const [database, username, envName] of [
  ["genieacs_manager", "manager", "MONGO_APP_PASSWORD"],
  ["genieacs", "acs", "MONGO_ACS_PASSWORD"]
]) {
  const password = process.env[envName];
  if (!password || !/^[a-fA-F0-9]{32,}$/.test(password)) throw new Error("Use a random hex database password");
  db.getSiblingDB(database).createUser({user: username, pwd: password, roles: [{role: "readWrite", db: database}]});
}
// Fail closed until authorized CPEs are configured with these credentials.
const cpeUser = process.env.CPE_USERNAME;
const cpePassword = process.env.CPE_PASSWORD;
if (!cpeUser || !cpePassword) throw new Error("CPE credentials are required");
db.getSiblingDB("genieacs").config.updateOne({_id: "cwmp.auth"}, {$set: {
  value: `AUTH(${JSON.stringify(cpeUser)}, ${JSON.stringify(cpePassword)})`
}}, {upsert: true});
