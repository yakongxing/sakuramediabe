# Catalog images

Movie covers, plot images, and actor avatars supplied as remote URLs by JavDB and
other remote metadata providers are stored and returned as validated third-party
HTTP(S) URLs. Those imports do not probe, download, stage, proxy, or publish the
images, and query strings are preserved byte-for-byte. External image URLs are
limited to 2048 UTF-8 bytes at the persistence boundary; the database columns
remain `VARCHAR(2048)` for compatibility.

Bundled metadata plugins use the stable host API to deliver local image file
artifacts (`cover_image_path` and `plot_image_paths`). The host imports those
artifacts into configured internal storage because the plugin supplies no
third-party image URL. This local-artifact contract remains supported and is not
part of direct hotlinking.

This hotlink architecture means clients disclose their IP address to the image
host. Availability, content changes, expiring URLs, rate limits, and anti-hotlink
rules are controlled by that host and are accepted tradeoffs.

Application-generated media thumbnails and other non-catalog assets remain in the
configured application storage and are exposed through signed file routes.
Existing internal catalog image keys remain readable and are converted naturally
when their movie is imported or strictly refreshed; there is no mass migration.

During upgrade, pending or running legacy `image_publication` queue rows are marked
failed with `catalog_image_publication_retired`. Staging directories are not swept
at startup. Rollback to code that assumes every `Image` value is a local key is not
safe after external URLs have been written.
