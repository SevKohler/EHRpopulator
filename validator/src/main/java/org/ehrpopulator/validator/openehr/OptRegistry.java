package org.ehrpopulator.validator.openehr;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.ehrbase.openehr.sdk.webtemplate.model.WebTemplate;
import org.ehrbase.openehr.sdk.webtemplate.parser.OPTParser;
import org.ehrbase.openehr.sdk.webtemplate.templateprovider.TemplateProvider;
import org.openehr.schemas.v1.OPERATIONALTEMPLATE;
import org.openehr.schemas.v1.TemplateDocument;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;

/**
 * In-memory registry of loaded OPTs.
 *
 * Stores both the raw OPERATIONALTEMPLATE (for TemplateProvider) and the
 * parsed WebTemplate (for validation and flat JSON deserialization).
 *
 * Implements TemplateProvider so it can be passed directly to FlatJasonProvider.
 */
@Component
public class OptRegistry implements TemplateProvider {

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
            byte[] bytes = optXml.getBytes(StandardCharsets.UTF_8);
            OPERATIONALTEMPLATE opt = TemplateDocument.Factory
                    .parse(new ByteArrayInputStream(bytes))
                    .getTemplate();
            String templateId = opt.getTemplateId().getValue();
            opts.put(templateId, opt);
            webTemplates.put(templateId, OPTParser.parse(opt));
            log.info("Registered OPT: {}", templateId);
        } catch (Exception e) {
            throw new RuntimeException("Failed to parse OPT XML: " + e.getMessage(), e);
        }
    }

    /** Required by TemplateProvider — used by FlatJasonProvider internally. */
    @Override
    public Optional<OPERATIONALTEMPLATE> find(String templateId) {
        return Optional.ofNullable(opts.get(templateId));
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
