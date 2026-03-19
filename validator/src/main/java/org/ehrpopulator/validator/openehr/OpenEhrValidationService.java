package org.ehrpopulator.validator.openehr;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.ehrpopulator.validator.api.ValidationRequest;
import org.ehrpopulator.validator.api.ValidationResponse;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Map;

/**
 * openEHR composition validation.
 *
 * Two-tier approach:
 *
 * 1. Structural validation via EHRbase SDK (offline, always available).
 *    Uses org.ehrbase.openehr.sdk:validation to check composition structure
 *    against a registered OPT.
 *
 * 2. Server-side validation via EHRbase REST API (optional, requires running EHRbase).
 *    POSTs the composition to a validation endpoint. EHRbase returns
 *    detailed errors including AQL path locations.
 *
 * If EHRbase is not configured, only SDK-level structural validation is performed.
 * Pass the OPT XML inline in the request to enable offline validation.
 */
@Service
public class OpenEhrValidationService {

    private static final Logger log = LoggerFactory.getLogger(OpenEhrValidationService.class);

    private final HttpClient httpClient = HttpClient.newHttpClient();
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final OptRegistry optRegistry;

    @Value("${validator.ehrbase.base-url:}")
    private String ehrbaseBaseUrl;

    @Value("${validator.ehrbase.username:ehrbase-user}")
    private String ehrbaseUsername;

    @Value("${validator.ehrbase.password:SuperSecretPassword}")
    private String ehrbasePassword;

    @Value("${validator.ehrbase.enabled:false}")
    private boolean ehrbaseEnabled;

    public OpenEhrValidationService(OptRegistry optRegistry) {
        this.optRegistry = optRegistry;
    }

    public ValidationResponse validate(ValidationRequest request) {
        List<ValidationResponse.Issue> issues = new ArrayList<>();

        // Register the inline OPT if provided
        if (request.getOptXml() != null && !request.getOptXml().isBlank()) {
            try {
                optRegistry.register(request.getOptXml());
            } catch (Exception e) {
                issues.add(new ValidationResponse.Issue("ERROR", "/",
                    "Failed to parse provided OPT XML: " + e.getMessage()));
                return new ValidationResponse(false, issues);
            }
        }

        // Tier 1: Offline structural validation via EHRbase SDK
        List<ValidationResponse.Issue> sdkIssues = sdkValidate(request);
        issues.addAll(sdkIssues);

        // Tier 2: Online EHRbase server validation (if configured)
        if (ehrbaseEnabled && ehrbaseBaseUrl != null && !ehrbaseBaseUrl.isBlank()) {
            List<ValidationResponse.Issue> serverIssues = serverValidate(request);
            issues.addAll(serverIssues);
        }

        boolean valid = issues.stream()
            .noneMatch(i -> "ERROR".equals(i.severity()) || "FATAL".equals(i.severity()));

        return new ValidationResponse(valid, issues);
    }

    /**
     * Offline validation using the EHRbase openEHR SDK.
     *
     * For canonical JSON: deserializes directly to Composition, then validates.
     * For flat JSON: uses the WebTemplate (built from the OPT) to deserialize
     *   flat paths to a Composition, then validates with CompositionValidator.
     */
    private List<ValidationResponse.Issue> sdkValidate(ValidationRequest request) {
        List<ValidationResponse.Issue> issues = new ArrayList<>();
        try {
            String templateId = optRegistry.detectTemplateId(request.getContent());
            if (templateId == null) {
                issues.add(new ValidationResponse.Issue("WARNING", "/archetype_details/template_id",
                    "Could not detect template ID from composition. Skipping SDK validation."));
                return issues;
            }

            var webTemplate = optRegistry.getWebTemplate(templateId);
            if (webTemplate == null) {
                issues.add(new ValidationResponse.Issue("WARNING", "/",
                    "OPT not registered for template '" + templateId +
                    "'. Pass opt_xml in request."));
                return issues;
            }
            var composition = parseComposition(request.getContent(), request.getFormat(), templateId);
            var validator = new org.ehrbase.openehr.sdk.validation.LocatableValidator();
            var results = validator.validate(composition, webTemplate);

            for (var r : results) {
                issues.add(new ValidationResponse.Issue(
                    "ERROR",
                    r.getAqlPath() != null ? r.getAqlPath() : "/",
                    r.getMessage() != null ? r.getMessage() : r.toString()
                ));
            }
        } catch (Exception e) {
            log.error("SDK validation error", e);
            issues.add(new ValidationResponse.Issue("ERROR", "/",
                "SDK validation exception: " + e.getMessage()));
        }
        return issues;
    }

    /**
     * Server-side validation via EHRbase REST API.
     * Posts the composition to EHRbase which performs full template-based validation.
     */
    private List<ValidationResponse.Issue> serverValidate(ValidationRequest request) {
        List<ValidationResponse.Issue> issues = new ArrayList<>();
        try {
            // Use the EHRbase composition validation endpoint
            // POST /ehrbase/rest/openehr/v1/composition (with a dedicated validation EHR)
            // EHRbase returns 400 with detailed validation errors on failure
            String url = ehrbaseBaseUrl.stripTrailing() +
                "/rest/openehr/v1/definition/template/adl1.4/" +
                optRegistry.detectTemplateId(request.getContent()) +
                "/example";

            // For actual composition validation, POST to a validation EHR:
            // We create or reuse a dedicated "validation EHR" for this purpose
            String validationEhrId = getOrCreateValidationEhr();
            if (validationEhrId == null) {
                issues.add(new ValidationResponse.Issue("WARNING", "/",
                    "Could not connect to EHRbase for server-side validation. SDK validation only."));
                return issues;
            }

            String compositionUrl = ehrbaseBaseUrl.stripTrailing() +
                "/rest/openehr/v1/ehr/" + validationEhrId + "/composition";

            String contentType = "OPENEHR_FLAT".equals(request.getFormat())
                ? "application/openehr.wt.flat.schema+json"
                : "application/json";

            HttpRequest httpRequest = HttpRequest.newBuilder()
                .uri(URI.create(compositionUrl))
                .header("Content-Type", contentType)
                .header("Accept", "application/json")
                .header("Authorization", basicAuth())
                // Return minimal response — we only care about validation errors
                .header("Prefer", "return=minimal")
                .POST(HttpRequest.BodyPublishers.ofString(request.getContent()))
                .build();

            HttpResponse<String> response = httpClient.send(httpRequest,
                HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));

            if (response.statusCode() == 201) {
                // Successfully committed — delete it immediately (it was just for validation)
                deleteComposition(validationEhrId, response.headers()
                    .firstValue("Location").orElse(null));
                // No additional issues from server
            } else if (response.statusCode() == 400 || response.statusCode() == 422) {
                parseEhrbaseErrors(response.body(), issues);
            } else {
                log.warn("Unexpected EHRbase response: {} {}", response.statusCode(), response.body());
                issues.add(new ValidationResponse.Issue("WARNING", "/",
                    "EHRbase returned HTTP " + response.statusCode() + " during server validation"));
            }

        } catch (Exception e) {
            log.error("EHRbase server validation error", e);
            issues.add(new ValidationResponse.Issue("WARNING", "/",
                "EHRbase server validation failed: " + e.getMessage()));
        }
        return issues;
    }

    /**
     * Deserialize composition JSON to a Composition RM object.
     *
     * Canonical JSON maps 1:1 to the RM structure — deserialized directly via CanonicalJson.
     * Flat JSON uses dot-notation paths from the web template — deserialized via
     * FlatJasonProvider (which uses OptRegistry as TemplateProvider to resolve paths).
     */
    private com.nedap.archie.rm.composition.Composition parseComposition(
            String content, String format, String templateId) throws Exception {
        if ("OPENEHR_FLAT".equals(format)) {
            var provider = new org.ehrbase.openehr.sdk.serialisation.flatencoding.FlatJasonProvider(
                    optRegistry);
            return provider
                    .buildFlatJson(org.ehrbase.openehr.sdk.serialisation.flatencoding.FlatFormat.SIM_SDT,
                            templateId)
                    .unmarshal(content);
        } else {
            return new org.ehrbase.openehr.sdk.serialisation.jsonencoding.CanonicalJson()
                    .unmarshal(content, com.nedap.archie.rm.composition.Composition.class);
        }
    }

    @SuppressWarnings("unchecked")
    private void parseEhrbaseErrors(String responseBody,
                                    List<ValidationResponse.Issue> issues) {
        try {
            Map<String, Object> body = objectMapper.readValue(responseBody, Map.class);
            // EHRbase error format: {"message": "...", "errors": [...]}
            Object message = body.get("message");
            if (message != null) {
                issues.add(new ValidationResponse.Issue("ERROR", "/", message.toString()));
            }
            Object errors = body.get("errors");
            if (errors instanceof List<?> errorList) {
                for (Object err : errorList) {
                    issues.add(new ValidationResponse.Issue("ERROR", "/", err.toString()));
                }
            }
        } catch (Exception e) {
            // Response wasn't JSON — use raw body
            issues.add(new ValidationResponse.Issue("ERROR", "/", responseBody));
        }
    }

    private String getOrCreateValidationEhr() {
        try {
            String url = ehrbaseBaseUrl.stripTrailing() + "/rest/openehr/v1/ehr";
            HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(url))
                .header("Content-Type", "application/json")
                .header("Authorization", basicAuth())
                .POST(HttpRequest.BodyPublishers.ofString(
                    "{\"_type\":\"EHR_STATUS\",\"is_modifiable\":true,\"is_queryable\":true}"))
                .build();
            HttpResponse<String> response = httpClient.send(request,
                HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 201) {
                // Extract EHR ID from Location header: .../ehr/{id}
                return response.headers().firstValue("ETag")
                    .map(e -> e.replace("\"", ""))
                    .orElse(null);
            }
        } catch (Exception e) {
            log.warn("Could not create validation EHR: {}", e.getMessage());
        }
        return null;
    }

    private void deleteComposition(String ehrId, String location) {
        if (location == null) return;
        try {
            HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(location))
                .header("Authorization", basicAuth())
                .DELETE()
                .build();
            httpClient.send(request, HttpResponse.BodyHandlers.discarding());
        } catch (Exception e) {
            log.debug("Could not delete validation composition: {}", e.getMessage());
        }
    }

    private String basicAuth() {
        String credentials = ehrbaseUsername + ":" + ehrbasePassword;
        return "Basic " + Base64.getEncoder().encodeToString(
            credentials.getBytes(StandardCharsets.UTF_8));
    }
}
