# External SQL procedure verification on IBM i

This fixture tests an RPG program and an external SQL procedure with the same
name. It requires the normal TOBi dependencies, the ILE RPG compiler, and access
to `QSYS2.SYSROUTINES` and `QSYS2.SYSSCHEMAS`. Use a development system and a new,
empty test library. The commands below create and change only the test objects
in `TBIPRCTST` and this fixture's local build files.

Run the commands in a Bash SSH session. From the root of the TOBi checkout being
tested:

```bash
export PATH=/QOpenSys/pkgs/bin:$PATH
export TOBI_TEST_CHECKOUT="$PWD"
cd tests/data/external_procedure
cl "CRTLIB LIB(TBIPRCTST) TEXT('TOBi procedure test')"
```

If `TBIPRCTST` already exists, choose a fresh library and update `objlib` and
`curlib` in `iproj.json`, the library in `sample.sqlprc`, and the commands below.
Use the explicit checkout path in every build command so the installed TOBi
version is not used accidentally.

## First and unchanged builds

```bash
"$TOBI_TEST_CHECKOUT/bin/makei" build
"$TOBI_TEST_CHECKOUT/bin/makei" build
cl "RUNSQL SQL('CALL TBIPRCTST.SAMPLE()') COMMIT(*NONE)"
```

The first build compiles `SAMPLE.PGM` and registers `SAMPLE.PROCEDURE`. The second
build performs neither operation. The call should succeed. In ACS Run SQL
Scripts, inspect the registration and keep the result for comparison:

```sql
SELECT SPECIFIC_SCHEMA, SPECIFIC_NAME, ROUTINE_BODY,
       ROUTINE_CREATED, LAST_ALTERED, EXTERNAL_NAME
  FROM QSYS2.SYSROUTINES
 WHERE SPECIFIC_SCHEMA = 'TBIPRCTST' AND SPECIFIC_NAME = 'SAMPLE'
   AND ROUTINE_TYPE = 'PROCEDURE';
```

The unchanged second build must leave the catalog timestamps unchanged.

## Source and implementation changes

Run each pair of builds separately:

```bash
sleep 1
touch sample.sqlprc
"$TOBI_TEST_CHECKOUT/bin/makei" build
"$TOBI_TEST_CHECKOUT/bin/makei" build

sleep 1
touch sample.pgm.rpgle
"$TOBI_TEST_CHECKOUT/bin/makei" build
"$TOBI_TEST_CHECKOUT/bin/makei" build
```

Touching the SQL source should register only the procedure. Touching the RPG
source should compile the program, then register the procedure. Each following
unchanged build should skip both operations.

## Dropped registration

```bash
cl "RUNSQL SQL('DROP SPECIFIC PROCEDURE TBIPRCTST.SAMPLE') COMMIT(*NONE)"
"$TOBI_TEST_CHECKOUT/bin/makei" build
"$TOBI_TEST_CHECKOUT/bin/makei" build
```

The first build should recreate the registration without recompiling the
program. The next build should skip both operations.

## Failed registration and retry

Append a call to a deliberately nonexistent procedure after the valid
registration. Keep a backup to restore this fixture's source:

```bash
cp sample.sqlprc sample.sqlprc.backup
printf '\nCALL TBIPRCTST.TOBI_INTENTIONAL_FAILURE();\n' >> sample.sqlprc
"$TOBI_TEST_CHECKOUT/bin/makei" build
echo "Build exit status: $?"
"$TOBI_TEST_CHECKOUT/bin/makei" build
echo "Retry exit status: $?"
```

Both builds should attempt registration and fail with a nonzero exit status,
even if `CREATE OR REPLACE PROCEDURE` succeeded before the failing call. There
must be no successful `SAMPLE.PROCEDURE` stamp under `.deps/procedures/`.

```bash
mv sample.sqlprc.backup sample.sqlprc
"$TOBI_TEST_CHECKOUT/bin/makei" build
"$TOBI_TEST_CHECKOUT/bin/makei" build
cl "RUNSQL SQL('CALL TBIPRCTST.SAMPLE()') COMMIT(*NONE)"
```

The repaired build should register successfully, the following build should skip
registration, and the call should succeed.

## Results to report

Record the tested commit (`git rev-parse HEAD`), IBM i release, Python and GNU
Make versions, and the outcome of each case. For failures, include the build
output and the relevant `.logs/` joblog and compiler output. If available, repeat
with an IASP using the project's normal `iasp` and `setIBMiEnvCmd` configuration.
Keep the test library until any failures have been investigated.
