# Render service

The service and compositions pin every direct Remotion package to `4.0.539`.
Keep those versions aligned when upgrading; regenerate and commit both
`render-service/package-lock.json` and `remotion/package-lock.json` together.
The Docker image uses Node 22 and installs both projects with `npm ci`, so a
clean image uses the committed dependency graph rather than resolving ranges.

From the repository root, verify a clean installation before building an image:

```bash
npm ci --prefix render-service
npm ci --prefix remotion
npm run build --prefix render-service
npm run build --prefix remotion
npm test --prefix render-service
docker build -f render-service/Dockerfile -t openshorts-renderer:check .
```

The metadata tests bundle the real composition and launch a local browser.
Remotion may download its browser on the first test or render when its cache
is empty; a successful image build alone does not prove offline readiness.
An image build does not replace a running renderer. Validate health and a
synthetic render in an isolated container before updating a deployment.
