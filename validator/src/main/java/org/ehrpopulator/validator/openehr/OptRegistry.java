package org.ehrpopulator.validator.openehr;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.openehr.schemas.v1.OPERATIONALTEMPLATE;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.xmlbeam.XBProjector;

import javax.xml.parsers.DocumentBuilderFactory;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory registry of loaded OPTs (Operational Templates).
 *
 * OPTs can be pre-loaded at startup from the templates/ directory,
 * or registered at runtime via the validation request's opt_xml field.
 *
 * Thread-safe for concurrent validation requests.
 */
@Component
public class OptRegistry {

    private static final Logger log = LoggerFactory.getLogger(OptRegistry.class);

    // templateId -> parsed OPERATIONALTEMPLATE
    private final Map<String, OPERATIONALTEMPLATE> templates = new ConcurrentHashMap<>();

    private final ObjectMapper objectMapper = new ObjectMapper();

    /**
     * Register an OPT from its raw XML string.
     * Idempotent — re-registering the same template ID overwrites the previous entry.
     */
    public void register(String optXml) {
        try {
            var factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            var builder = factory.newDocumentBuilder();
            var document = builder.parse(new ByteArrayInputStream(
                optXml.getBytes(StandardCharsets.UTF_8)));

            // Use EHRbase SDK to parse OPT XML
            var opt = org.ehrbase.openehr.sdk.opt.normalizer.OptNormalizer.parse(optXml);
            String templateId = opt.getTemplateId().getValue();
            templates.put(templateId, opt);
            log.info("Registered OPT: {}", templateId);
        } catch (Exception e) {
            throw new RuntimeException("Failed to parse OPT XML: " + e.getMessage(), e);
        }
    }

    /**
     * Look up a registered OPT by template ID.
     * Returns null if not registered.
     */
    public OPERATIONALTEMPLATE getOpt(String templateId) {
        return templates.get(templateId);
    }

    /**
     * Detect the template ID from a composition.
     *
     * For canonical JSON compositions, looks at:
     * $.archetype_details.template_id.value
     *
     * For flat JSON compositions, looks at keys like
     * ctx/template_id or _template_id.
     */
    public String detectTemplateId(String compositionJson) {
        try {
            JsonNode root = objectMapper.readTree(compositionJson);

            // Canonical JSON path
            JsonNode templateIdNode = root
                .path("archetype_details")
                .path("template_id")
                .path("value");
            if (!templateIdNode.isMissingNode()) {
                return templateIdNode.asText();
            }

            // Flat JSON: ctx/template_id
            JsonNode ctxTemplateId = root.path("ctx/template_id");
            if (!ctxTemplateId.isMissingNode()) {
                return ctxTemplateId.asText();
            }

            // Flat JSON: _template_id at top level
            JsonNode underscoreTemplateId = root.path("_template_id");
            if (!underscoreTemplateId.isMissingNode()) {
                return underscoreTemplateId.asText();
            }

        } catch (Exception e) {
            log.warn("Could not detect template ID from composition: {}", e.getMessage());
        }
        return null;
    }

    public Map<String, OPERATIONALTEMPLATE> getAll() {
        return Map.copyOf(templates);
    }
}
