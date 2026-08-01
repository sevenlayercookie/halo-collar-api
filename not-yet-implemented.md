I treated trailing slashes and placeholder names as equivalent for the comparison. The APK’s declarations themselves omit terminal slashes.

| HTTP | Missing path |
|---|---|
| POST | `/account/user-experience-evaluation` |
| POST | `/account/send-ecommerce-login-link-via-email` |
| POST | `/collar/{id}/encrypt-wifi-password` |
| POST | `/collar/{id}/reset-issues` |
| GET | `/collar/dgnss` |
| POST | `/mapbox/request` |
| PUT | `/mapbox/request/{id}` |
| DELETE | `/mapbox/request/{id}` |
| GET | `/mapbox/request/{id}/map-sources` |
| PUT | `/pet/{id}/icon` |
| DELETE | `/pet/{id}/icon` |
| POST | `/pet/statistics` |
| PUT | `/portal-notification/{id}/read` |
| GET | `/portal-notification/view/in-app/{id}` |
| POST | `/collar/{id}/rolling-codes` |
| POST | `/subscription/my/refresh` |
| POST | `/subscription/my/log-prompting-screen-viewing` |
| GET | `/training/my` |
| GET | `/training/user/course-launch-link/{courseId}` |
| GET | `/training/pet/{petId}/course-launch-link/{courseId}` |
| POST | `/training/user/send-course-launch-link-via-email/{courseId}` |
| POST | `/training/pet/{petId}/send-course-launch-link-via-email/{courseId}` |
| PUT | `/training/user/{courseId}/{lessonType}/complete` |
| DELETE | `/notification/my` |
| PUT | `/notification/my/status` |
| PUT | `/walk/{walkId}/failed/trail-data` |
| GET | `/walk/{walkId}/failed/trail-data` |
| DELETE | `/walk/{walkId}/failed/trail-data` |
| POST | `/events` |
| GET | `/jotform` |

`/events` and `/jotform` use the app’s Beam client, whose static base is `https://app-braze-halo-prod.azurewebsites.net`.

Two documentation refinements:

- Your existing two-parameter training route structurally matches the APK’s `GET /training/user/course-launch-link/{courseId}/{lessonType}`.
- The APK defines `POST /report-all/{url}`; its current call site supplies `url = "api/parcels"`, so your concrete `/report-all/api/parcels` is valid.

There are also direct external REST clients not represented in your list:

| Service | HTTP | Path |
|---|---|---|
| Mapbox | GET | `https://api.mapbox.com/geocoding/v5/mapbox.places/{coordinates}.json` |
| Mapbox | GET | `https://api.mapbox.com/geocoding/v5/mapbox.places/{search_text}.json` |
| Mapbox | GET | `https://api.mapbox.com/search/searchbox/v1/reverse` |
| Mapbox | GET | `https://api.mapbox.com/styles/v1/mapbox/light-v11/static/{features}/auto/{size}@2x` |
| Dog Park | GET | `{DogParkBaseUrl}/configuration` |
| Dog Park | GET | `{DogParkBaseUrl}/zoom/token/sdk` |
| Dog Park | GET | `{DogParkBaseUrl}/status` |
| Dog Park | POST | `{DogParkBaseUrl}/feedback` |
| Dog Park | POST | `{DogParkBaseUrl}/feedback/check` |

The app also constructs a map-tile resource URL: `/map-tiles/{z}/{x}/{y}.png`.

SignalR is not limited to two hubs in this build:

| Connection | Hub path | Registered client targets |
|---|---|---|
| Default | `{SocketBaseUrl}/NotificationHub` | `HandleMapUpdateRequestCompleted`, `HandleDataStateChanged`, `HandleCollarDataSynchronized` |
| Telemetry | `{SocketBaseUrl}/TelemetryHub` | `HandleIoTTelemetry`, `HandleBleTelemetry`; also uses `BroadcastBleTelemetry`, `ReceiveTelemetryHeartbeat`, `InactivateConnection` |
| Dog Park | `{DogParkBaseUrl}/meetings-live` | `DogParkMeetingsSetChanged`, `ZoomMeetingsSetChanged`, `AnnouncementsChanged`, `LiveConfigChanged` |

The standard SignalR negotiation applies to all three hubs. In this APK, the Azure sockets hostname is not hard-coded; the roots are configuration-driven (`BaseUrl`, `AuthBaseUrl`, `SocketBaseUrl`, `DogParkBaseUrl`, `MapBoxBaseUrl`, `BeamBaseUrl`) and can be overridden at runtime.
