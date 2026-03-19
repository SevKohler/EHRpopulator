package org.ehrpopulator.validator.fhir;

import ca.uhn.fhir.context.FhirContext;
import ca.uhn.fhir.context.support.DefaultProfileValidationSupport;
import ca.uhn.fhir.context.support.IValidationSupport;
import ca.uhn.fhir.validation.FhirValidator;
import ca.uhn.fhir.validation.ResultSeverityEnum;
import ca.uhn.fhir.validation.ValidationResult;
import org.ehrpopulator.validator.api.ValidationRequest;
import org.ehrpopulator.validator.api.ValidationResponse;
import org.hl7.fhir.common.hapi.validation.support.CachingValidationSupport;
import org.hl7.fhir.common.hapi.validation.support.CommonCodeSystemsTerminologyService;
import org.hl7.fhir.common.hapi.validation.support.InMemoryTerminologyServerValidationSupport;
import org.hl7.fhir.common.hapi.validation.support.PrePopulatedValidationSupport;
import org.hl7.fhir.common.hapi.validation.support.RemoteTerminologyServiceValidationSupport;
import org.hl7.fhir.common.hapi.validation.support.SnapshotGeneratingValidationSupport;
import org.hl7.fhir.common.hapi.validation.support.ValidationSupportChain;
import org.hl7.fhir.common.hapi.validation.validator.FhirInstanceValidator;
import org.hl7.fhir.r4.model.StructureDefinition;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import java.util.List;

/**
 * FHIR R4 validation using HAPI FHIR.
 *
 * Validates against:
 * 1. Base R4 specification
 * 2. Any StructureDefinition passed inline in the request
 * 3. Remote terminology server (configurable) for code validation
 *
 * The FhirContext and base validation support are initialized once at startup
 * (expensive operation) and cached. Per-request StructureDefinitions are
 * handled via a PrePopulatedValidationSupport layer.
 */
@Service
public class FhirValidationService {

    private static final Logger log = LoggerFactory.getLogger(FhirValidationService.class);

    private final FhirContext fhirContext;
    private final IValidationSupport baseValidationSupport;

    @Value("${validator.fhir.terminology-server-url:https://tx.fhir.org/r4}")
    private String terminologyServerUrl;

    public FhirValidationService() {
        log.info("Initializing HAPI FHIR context (R4)...");
        this.fhirContext = FhirContext.forR4();

        // Build the base validation support chain (shared, cached across requests)
        ValidationSupportChain baseChain = new ValidationSupportChain(
            new DefaultProfileValidationSupport(fhirContext),
            new InMemoryTerminologyServerValidationSupport(fhirContext),
            new CommonCodeSystemsTerminologyService(fhirContext),
            new SnapshotGeneratingValidationSupport(fhirContext)
        );
        this.baseValidationSupport = new CachingValidationSupport(baseChain);
        log.info("HAPI FHIR initialized.");
    }

    public ValidationResponse validate(ValidationRequest request) {
        FhirValidator validator = buildValidator(request);

        try {
            var resource = fhirContext.newJsonParser().parseResource(request.getContent());
            ValidationResult result = validator.validateWithResult(resource);

            List<ValidationResponse.Issue> issues = result.getMessages().stream()
                .map(msg -> new ValidationResponse.Issue(
                    msg.getSeverity().name(),
                    msg.getLocationString(),
                    msg.getMessage()
                ))
                .toList();

            boolean valid = result.getMessages().stream()
                .noneMatch(m -> m.getSeverity() == ResultSeverityEnum.ERROR
                             || m.getSeverity() == ResultSeverityEnum.FATAL);

            return new ValidationResponse(valid, issues);

        } catch (Exception e) {
            log.error("FHIR parse/validation error", e);
            return new ValidationResponse(false,
                List.of(new ValidationResponse.Issue("ERROR", "/",
                    "Parse error: " + e.getMessage())));
        }
    }

    private FhirValidator buildValidator(ValidationRequest request) {
        // Start with the shared cached base support
        ValidationSupportChain chain;

        if (request.getStructureDefinitionJson() != null && !request.getStructureDefinitionJson().isBlank()) {
            // Register the inline StructureDefinition for this request
            PrePopulatedValidationSupport prePopulated = new PrePopulatedValidationSupport(fhirContext);
            var sd = (StructureDefinition) fhirContext.newJsonParser()
                .parseResource(request.getStructureDefinitionJson());
            prePopulated.addStructureDefinition(sd);
            log.debug("Registered inline StructureDefinition: {}", sd.getUrl());

            chain = new ValidationSupportChain(prePopulated, baseValidationSupport);
        } else {
            chain = new ValidationSupportChain(baseValidationSupport);
        }

        // Optionally wire in a remote terminology server for code validation
        if (terminologyServerUrl != null && !terminologyServerUrl.isBlank()) {
            RemoteTerminologyServiceValidationSupport remote =
                new RemoteTerminologyServiceValidationSupport(fhirContext, terminologyServerUrl);
            chain = new ValidationSupportChain(remote, chain);
        }

        FhirInstanceValidator module = new FhirInstanceValidator(new CachingValidationSupport(chain));
        // Allow unknown extensions from implementation guides not loaded locally
        module.setNoTerminologyChecks(false);
        module.setAnyExtensionsAllowed(true);

        FhirValidator validator = fhirContext.newValidator();
        validator.registerValidatorModule(module);
        return validator;
    }
}
