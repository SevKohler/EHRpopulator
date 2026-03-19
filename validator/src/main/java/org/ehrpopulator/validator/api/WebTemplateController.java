package org.ehrpopulator.validator.api;

import org.ehrpopulator.validator.openehr.WebTemplateConverter;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

/**
 * Converts an OPT XML to a web template JSON using the EHRbase SDK.
 *
 * POST /webtemplate
 *   Content-Type: application/xml
 *   Body: raw OPT XML
 *   Returns: web template JSON (EHRbase openehr.wt+json format)
 *
 * The Python pipeline calls this when given a raw OPT file, so template
 * analysis is always done from the richer web template format — no LLM
 * needed for OPT parsing, no running EHRbase server required.
 */
@RestController
public class WebTemplateController {

    private static final Logger log = LoggerFactory.getLogger(WebTemplateController.class);

    private final WebTemplateConverter converter;

    public WebTemplateController(WebTemplateConverter converter) {
        this.converter = converter;
    }

    @PostMapping(
        value = "/webtemplate",
        consumes = {MediaType.APPLICATION_XML_VALUE, "application/openehr+xml", "text/xml"},
        produces = MediaType.APPLICATION_JSON_VALUE
    )
    public ResponseEntity<String> convert(@RequestBody String optXml) {
        log.info("Converting OPT XML to web template");
        try {
            String webTemplate = converter.toWebTemplate(optXml);
            return ResponseEntity.ok()
                .contentType(MediaType.APPLICATION_JSON)
                .body(webTemplate);
        } catch (Exception e) {
            log.error("OPT to web template conversion failed", e);
            return ResponseEntity.badRequest()
                .body("{\"error\": \"" + e.getMessage().replace("\"", "'") + "\"}");
        }
    }
}
