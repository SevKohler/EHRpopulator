package org.ehrpopulator.validator.api;

import org.ehrpopulator.validator.fhir.FhirValidationService;
import org.ehrpopulator.validator.openehr.OpenEhrValidationService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/**
 * REST endpoint consumed by the Python agent pipeline.
 *
 * POST /validate — validate a single resource
 * GET  /health   — liveness check
 */
@RestController
public class ValidationController {

    private static final Logger log = LoggerFactory.getLogger(ValidationController.class);

    private final FhirValidationService fhirService;
    private final OpenEhrValidationService openEhrService;

    public ValidationController(FhirValidationService fhirService,
                                 OpenEhrValidationService openEhrService) {
        this.fhirService = fhirService;
        this.openEhrService = openEhrService;
    }

    @PostMapping("/validate")
    public ResponseEntity<ValidationResponse> validate(@RequestBody ValidationRequest request) {
        log.info("Validating resource, format={}", request.getFormat());

        if (request.getContent() == null || request.getContent().isBlank()) {
            return ResponseEntity.badRequest()
                .body(new ValidationResponse(false,
                    java.util.List.of(new ValidationResponse.Issue("ERROR", "/", "content is required"))));
        }

        ValidationResponse response = switch (request.getFormat()) {
            case "FHIR_R4" -> fhirService.validate(request);
            case "OPENEHR_FLAT", "OPENEHR_CANONICAL" -> openEhrService.validate(request);
            default -> new ValidationResponse(false,
                java.util.List.of(new ValidationResponse.Issue("ERROR", "/",
                    "Unknown format: " + request.getFormat() +
                    ". Supported: FHIR_R4, OPENEHR_FLAT, OPENEHR_CANONICAL")));
        };

        log.info("Validation result: valid={}, issues={}", response.isValid(), response.getIssueCount());
        return ResponseEntity.ok(response);
    }

    @GetMapping("/health")
    public ResponseEntity<String> health() {
        return ResponseEntity.ok("ok");
    }
}
