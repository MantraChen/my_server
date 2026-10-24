// Do not mutate the shared Node runtime used by the other server components.
const [major, minor, patch] = process.versions.node.split('.').map(Number);
if (major !== 24 || minor < 22 || (minor === 22 && patch < 1)) {
  throw new Error('Dev Blog requires a dedicated Node 24 runtime, at least 24.22.1');
}
