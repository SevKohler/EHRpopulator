package org.ehrpopulator.validator.api;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.ehrbase.openehr.sdk.serialisation.flatencoding.FlatFormat;
import org.ehrbase.openehr.sdk.serialisation.flatencoding.FlatJasonProvider;
import org.ehrbase.openehr.sdk.serialisation.jsonencoding.CanonicalJson;
import org.ehrpopulator.validator.openehr.OptRegistry;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * POST /to-canonical
 *
 * Converts an EHRbase FLAT JSON composition to canonical openEHR JSON using the SDK.
 * The Python pipeline always generates FLAT internally, then calls this endpoint when
 * the user requested OPENEHR_CANONICAL output format.
 *
 * Flow:
 *   1. Register the OPT (if provided) so the flat-path resolver can find template paths
 *   2. Unmarshal FLAT JSON → Composition RM object via FlatJasonProvider
 *   3. Marshal Composition → canonical JSON via CanonicalJson
 */
@RestController
public class ToCanonicalController {

    private static final Logger log = LoggerFactory.getLogger(ToCanonicalController.class);

    private final OptRegistry optRegistry;
    private final ObjectMapper objectMapper = new ObjectMapper();

    public ToCanonicalController(OptRegistry optRegistry) {
        this.optRegistry = optRegistry;
    }

    @PostMapping(
        value = "/to-canonical",
        consumes = MediaType.APPLICATION_JSON_VALUE,
        produces = MediaType.APPLICATION_JSON_VALUE
    )
    public ResponseEntity<String> toCanonical(@RequestBody ToCanonicalRequest request) {
        if (request.getFlatJson() == null || request.getFlatJson().isBlank()) {
            return ResponseEntity.badRequest()
                .body("{\"error\": \"flat_json is required\"}");
        }

        // Register OPT so the SDK can resolve template paths
        if (request.getOptXml() != null && !request.getOptXml().isBlank()) {
            try {
                optRegistry.register(request.getOptXml());
            } catch (Exception e) {
                log.error("Failed to register OPT", e);
                return ResponseEntity.badRequest()
                    .body("{\"error\": \"Failed to parse OPT XML: " +
                          e.getMessage().replace("\"", "'") + "\"}");
            }
        }

        try {
            String templateId = request.getTemplateId() != null && !request.getTemplateId().isBlank()
                ? request.getTemplateId()
                : optRegistry.detectTemplateId(request.getFlatJson());
            if (templateId == null) {
                return ResponseEntity.badRequest()
                    .body("{\"error\": \"Could not detect template ID. Pass template_id in the request.\"}");
            }

            // Flat JSON → Composition RM object
            var composition = new FlatJasonProvider(optRegistry)
                .buildFlatJson(FlatFormat.SIM_SDT, templateId)
                .unmarshal(request.getFlatJson());

            // Composition RM object → canonical JSON
            String canonical = new CanonicalJson().marshal(composition);

            // Pretty-print
            Object parsed = objectMapper.readValue(canonical, Object.class);
            String pretty = objectMapper.writerWithDefaultPrettyPrinter().writeValueAsString(parsed);

            log.info("Converted flat → canonical for template '{}'", templateId);
            return ResponseEntity.ok()
                .contentType(MediaType.APPLICATION_JSON)
                .body(pretty);

        } catch (Exception e) {
            log.error("Flat to canonical conversion failed", e);
            return ResponseEntity.badRequest()
                .body("{\"error\": \"Conversion failed: " +
                      e.getMessage().replace("\"", "'") + "\"}");
        }
    }
}
