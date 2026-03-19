package org.ehrpopulator.validator.openehr;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.ehrbase.openehr.sdk.webtemplate.builder.WebTemplateBuilder;
import org.ehrbase.openehr.sdk.webtemplate.model.WebTemplate;
import org.openehr.schemas.v1.OPERATIONALTEMPLATE;
import org.openehr.schemas.v1.TemplateDocument;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory registry of loaded OPTs (Operational Templates).
 *
 * OPTs can be registered at runtime via the validation request's opt_xml field.
 * Both the parsed OPERATIONALTEMPLATE (for CompositionValidator) and the WebTemplate
 * (for flat JSON deserialization) are stored per template ID.
 *
 * Thread-safe for concurrent validation requests.
 */
@Component
public class OptRegistry {

    private static final Logger log = LoggerFactory.getLogger(OptRegistry.class);

    private final Map<String, OPERATIONALTEMPLATE> opts = new ConcurrentHashMap<>();
    private final Map<String, WebTemplate> webTemplates = new ConcurrentHashMap<>();

    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * Register an OPT from its raw XML string.
     * Idempotent — re-registering the same template ID overwrites the previous entry.
     */
    public void register(String optXml) {
        try {
            OPERATIONALTEMPLATE opt = TemplateDocument.Factory.parse(
                    new ByteArrayInputStream(optXml.getBytes(StandardCharsets.UTF_8))
            ).getTemplate();

            String templateId = opt.getTemplateId().getValue();
            opts.put(templateId, opt);
            webTemplates.put(templateId, new WebTemplateBuilder().build(opt, false));
            log.info("Registered OPT: {}", templateId);
        } catch (Exception e) {
            throw new RuntimeException("Failed to parse OPT XML: " + e.getMessage(), e);
        }
    }

    public OPERATIONALTEMPLATE getOpt(String templateId) {
        return opts.get(templateId);
    }

    public WebTemplate getWebTemplate(String templateId) {
        return webTemplates.get(templateId);
    }

    /**
     * Detect the template ID from a composition (canonical or flat JSON).
     *
     * Canonical JSON: $.archetype_details.template_id.value
     * Flat JSON:      ctx/template_id  or  _template_id
     */
    public String detectTemplateId(String compositionJson) {
        try {
            JsonNode root = objectMapper.readTree(compositionJson);

            JsonNode canonical = root.path("archetype_details").path("template_id").path("value");
            if (!canonical.isMissingNode()) return canonical.asText();

            JsonNode ctx = root.path("ctx/template_id");
            if (!ctx.isMissingNode()) return ctx.asText();

            JsonNode underscore = root.path("_template_id");
            if (!underscore.isMissingNode()) return underscore.asText();

        } catch (Exception e) {
            log.warn("Could not detect template ID from composition: {}", e.getMessage());
        }
        return null;
    }

    public Map<String, OPERATIONALTEMPLATE> getAll() {
        return Map.copyOf(opts);
    }
}
