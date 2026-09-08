# Firebase — Rules, Indexes, and Emulator Tests

This folder owns the Firebase **server-side** configuration for the
Multi-Broker Portfolio Tracker. Client SDK config (plist / json /
`firebase_options.dart`) is generated separately — see
[CLIENT_CONFIG.md](./CLIENT_CONFIG.md).

## Layout

```
firebase/
├── firebase.json              # rules + indexes + emulator wiring
├── .firebaserc                # default project alias
├── firestore.rules            # security rules
├── firestore.indexes.json     # composite index definitions
├── package.json               # mocha + @firebase/rules-unit-testing
├── tests/
│   └── firestore.rules.test.js
├── CLIENT_CONFIG.md
└── README.md
```

## Run the rules tests

```bash
cd firebase
npm install
npm run test:emulator      # boots Firestore + Auth emulators and runs mocha
```

`npm run test:emulator` shells out to:

```
firebase emulators:exec --only firestore,auth --project mbp-tracker-test "npm test"
```

Java 11+ is required by the Firebase emulator suite.

## Deploy rules and indexes

```bash
cd firebase
firebase deploy --only firestore:rules,firestore:indexes --project mbp-tracker-dev
```
