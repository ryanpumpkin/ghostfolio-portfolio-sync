/**
 * Emulator-based unit tests for firestore.rules.
 *
 * Run with:
 *   cd firebase
 *   npm install
 *   npm run test:emulator
 *
 * Or, if a Firestore + Auth emulator is already running on the default
 * ports, simply:
 *   npm test
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {
  initializeTestEnvironment,
  assertSucceeds,
  assertFails,
} = require('@firebase/rules-unit-testing');
const { setDoc, getDoc, doc } = require('firebase/firestore');

const PROJECT_ID = 'mbp-tracker-test';

let testEnv;

before(async function () {
  this.timeout(20000);
  testEnv = await initializeTestEnvironment({
    projectId: PROJECT_ID,
    firestore: {
      rules: fs.readFileSync(
        path.join(__dirname, '..', 'firestore.rules'),
        'utf8'
      ),
      host: '127.0.0.1',
      port: 8080,
    },
  });
});

after(async () => {
  if (testEnv) await testEnv.cleanup();
});

beforeEach(async () => {
  await testEnv.clearFirestore();
});

describe('users/{uid} ownership', () => {
  it('owner can read and write their own user doc', async () => {
    const alice = testEnv.authenticatedContext('alice').firestore();
    await assertSucceeds(
      setDoc(doc(alice, 'users/alice'), { baseCurrency: 'HKD' })
    );
    await assertSucceeds(getDoc(doc(alice, 'users/alice')));
  });

  it('non-owner cannot read another user doc', async () => {
    // Seed via privileged context first.
    await testEnv.withSecurityRulesDisabled(async (ctx) => {
      await setDoc(doc(ctx.firestore(), 'users/alice'), {
        baseCurrency: 'HKD',
      });
    });
    const bob = testEnv.authenticatedContext('bob').firestore();
    await assertFails(getDoc(doc(bob, 'users/alice')));
  });

  it('non-owner cannot write into another user subcollection', async () => {
    const bob = testEnv.authenticatedContext('bob').firestore();
    await assertFails(
      setDoc(doc(bob, 'users/alice/connections/c1'), {
        kind: 'longbridge',
      })
    );
  });

  it('unauthenticated user cannot read/write user docs', async () => {
    const anon = testEnv.unauthenticatedContext().firestore();
    await assertFails(getDoc(doc(anon, 'users/alice')));
    await assertFails(
      setDoc(doc(anon, 'users/alice'), { baseCurrency: 'HKD' })
    );
  });

  it('owner can write into nested subcollections (alerts, manual_holdings)', async () => {
    const alice = testEnv.authenticatedContext('alice').firestore();
    await assertSucceeds(
      setDoc(doc(alice, 'users/alice/alerts/a1'), {
        kind: 'price',
        scope: 'AAPL',
        threshold: 200,
        active: true,
      })
    );
    await assertSucceeds(
      setDoc(doc(alice, 'users/alice/manual_holdings/h1'), {
        symbol: 'GOLD',
        quantity: 1.5,
      })
    );
  });
});

describe('fx_rates shared cache', () => {
  it('authed user can read fx_rates', async () => {
    await testEnv.withSecurityRulesDisabled(async (ctx) => {
      await setDoc(doc(ctx.firestore(), 'fx_rates/USD_HKD'), {
        rate: 7.8,
        fetched_at: Date.now(),
      });
    });
    const alice = testEnv.authenticatedContext('alice').firestore();
    await assertSucceeds(getDoc(doc(alice, 'fx_rates/USD_HKD')));
  });

  it('regular authed user cannot write fx_rates', async () => {
    const alice = testEnv.authenticatedContext('alice').firestore();
    await assertFails(
      setDoc(doc(alice, 'fx_rates/USD_HKD'), { rate: 7.8 })
    );
  });

  it('service-account token may write fx_rates', async () => {
    const svc = testEnv
      .authenticatedContext('backend-svc', { service_account: true })
      .firestore();
    await assertSucceeds(
      setDoc(doc(svc, 'fx_rates/USD_HKD'), {
        rate: 7.8,
        fetched_at: Date.now(),
      })
    );
  });

  it('unauthenticated user cannot read fx_rates', async () => {
    const anon = testEnv.unauthenticatedContext().firestore();
    await assertFails(getDoc(doc(anon, 'fx_rates/USD_HKD')));
  });
});

describe('default deny', () => {
  it('unrelated top-level collection rejects all access', async () => {
    const alice = testEnv.authenticatedContext('alice').firestore();
    await assertFails(getDoc(doc(alice, 'random/whatever')));
    await assertFails(
      setDoc(doc(alice, 'random/whatever'), { x: 1 })
    );
  });
});
