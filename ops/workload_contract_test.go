package ops

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"strconv"
	"strings"
	"testing"
)

const pinnedImage = "eceasy/cli-proxy-api@sha256:e75d910b1fa7ef7e05cf3d5b06c3fbaa467eaf70cea0232cb37e4ede19e978ed"

var (
	errDuplicateJSONObjectMember  = errors.New("duplicate JSON object member")
	errDuplicateExposureTuple     = errors.New("duplicate expected_lan_exposure tuple")
	errInvalidJSONFieldName       = errors.New("contract contains an invalid JSON field name")
	errInvalidJSONStructure       = errors.New("invalid JSON structure")
	errMissingRequiredJSONField   = errors.New("contract is missing a required JSON field")
	errNonContractedPublishedPort = errors.New("tracked Compose file contains a non-contracted published port")
	errNullJSONValue              = errors.New("contract contains a null JSON value")
)

type jsonShape struct {
	object map[string]jsonShape
	array  *jsonShape
	scalar bool
}

type workloadContract struct {
	SchemaVersion int              `json:"schema_version"`
	Workload      contractWorkload `json:"workload"`
}

type contractWorkload struct {
	ID                  string             `json:"id"`
	PhysicalHost        string             `json:"physical_host"`
	RunsOn              string             `json:"runs_on"`
	Lifecycle           string             `json:"lifecycle"`
	Runtime             contractRuntime    `json:"runtime"`
	ExpectedLANExposure []contractExposure `json:"expected_lan_exposure"`
	Health              contractHealth     `json:"health"`
}

type contractRuntime struct {
	Kind          string `json:"kind"`
	ComposeFile   string `json:"compose_file"`
	Service       string `json:"service"`
	ContainerName string `json:"container_name"`
	Image         string `json:"image"`
	RestartPolicy string `json:"restart_policy"`
}

type contractExposure struct {
	Protocol string `json:"protocol"`
	Port     int    `json:"port"`
	Scope    string `json:"scope"`
	Purpose  string `json:"purpose"`
}

type contractHealth struct {
	Docker contractDockerHealth `json:"docker"`
	HTTP   contractHTTPHealth   `json:"http"`
}

type contractDockerHealth struct {
	ContainerName  string `json:"container_name"`
	ExpectedState  string `json:"expected_state"`
	ExpectedHealth string `json:"expected_health"`
}

type contractHTTPHealth struct {
	Scheme         string `json:"scheme"`
	Host           string `json:"host"`
	Port           int    `json:"port"`
	Path           string `json:"path"`
	ExpectedStatus int    `json:"expected_status"`
}

func rejectDuplicateJSONKeys(contractData []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(contractData))
	decoder.UseNumber()
	if err := scanJSONValue(decoder); err != nil {
		return err
	}
	if _, err := decoder.Token(); errors.Is(err, io.EOF) {
		return nil
	}
	return errInvalidJSONStructure
}

func scanJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return errInvalidJSONStructure
	}
	delimiter, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		return nil
	}

	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return errInvalidJSONStructure
			}
			key, ok := keyToken.(string)
			if !ok {
				return errInvalidJSONStructure
			}
			if _, duplicate := seen[key]; duplicate {
				return errDuplicateJSONObjectMember
			}
			seen[key] = struct{}{}
			if err := scanJSONValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return errInvalidJSONStructure
		}
		return nil
	case '[':
		for decoder.More() {
			if err := scanJSONValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errInvalidJSONStructure
		}
		return nil
	default:
		return errInvalidJSONStructure
	}
}

func workloadContractJSONShape() jsonShape {
	scalar := jsonShape{scalar: true}
	exposure := jsonShape{object: map[string]jsonShape{
		"protocol": scalar,
		"port":     scalar,
		"scope":    scalar,
		"purpose":  scalar,
	}}
	return jsonShape{object: map[string]jsonShape{
		"schema_version": scalar,
		"workload": {object: map[string]jsonShape{
			"id":            scalar,
			"physical_host": scalar,
			"runs_on":       scalar,
			"lifecycle":     scalar,
			"runtime": {object: map[string]jsonShape{
				"kind":           scalar,
				"compose_file":   scalar,
				"service":        scalar,
				"container_name": scalar,
				"image":          scalar,
				"restart_policy": scalar,
			}},
			"expected_lan_exposure": {array: &exposure},
			"health": {object: map[string]jsonShape{
				"docker": {object: map[string]jsonShape{
					"container_name":  scalar,
					"expected_state":  scalar,
					"expected_health": scalar,
				}},
				"http": {object: map[string]jsonShape{
					"scheme":          scalar,
					"host":            scalar,
					"port":            scalar,
					"path":            scalar,
					"expected_status": scalar,
				}},
			}},
		}},
	}}
}

func validateContractJSONShape(contractData []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(contractData))
	decoder.UseNumber()
	if err := scanJSONShapeValue(decoder, workloadContractJSONShape()); err != nil {
		return err
	}
	if _, err := decoder.Token(); errors.Is(err, io.EOF) {
		return nil
	}
	return errInvalidJSONStructure
}

func scanJSONShapeValue(decoder *json.Decoder, shape jsonShape) error {
	token, err := decoder.Token()
	if err != nil {
		return errInvalidJSONStructure
	}
	if shape.scalar {
		if _, isDelimiter := token.(json.Delim); isDelimiter {
			return errInvalidJSONStructure
		}
		return nil
	}

	delimiter, isDelimiter := token.(json.Delim)
	if !isDelimiter {
		return errInvalidJSONStructure
	}
	if shape.object != nil {
		if delimiter != json.Delim('{') {
			return errInvalidJSONStructure
		}
		seen := make(map[string]struct{}, len(shape.object))
		for decoder.More() {
			keyToken, err := decoder.Token()
			if err != nil {
				return errInvalidJSONStructure
			}
			key, ok := keyToken.(string)
			if !ok {
				return errInvalidJSONStructure
			}
			fieldShape, allowed := shape.object[key]
			if !allowed {
				return errInvalidJSONFieldName
			}
			seen[key] = struct{}{}
			if err := scanJSONShapeValue(decoder, fieldShape); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return errInvalidJSONStructure
		}
		if len(seen) != len(shape.object) {
			return errMissingRequiredJSONField
		}
		return nil
	}
	if shape.array != nil {
		if delimiter != json.Delim('[') {
			return errInvalidJSONStructure
		}
		for decoder.More() {
			if err := scanJSONShapeValue(decoder, *shape.array); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errInvalidJSONStructure
		}
		return nil
	}
	return errInvalidJSONStructure
}

func rejectJSONNullValues(contractData []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(contractData))
	decoder.UseNumber()
	for {
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return errInvalidJSONStructure
		}
		if token == nil {
			return errNullJSONValue
		}
	}
}

func decodeWorkloadContract(contractData []byte) (workloadContract, error) {
	if err := rejectDuplicateJSONKeys(contractData); err != nil {
		return workloadContract{}, err
	}
	if err := validateContractJSONShape(contractData); err != nil {
		return workloadContract{}, err
	}
	if err := rejectJSONNullValues(contractData); err != nil {
		return workloadContract{}, err
	}
	var contract workloadContract
	decoder := json.NewDecoder(bytes.NewReader(contractData))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&contract); err != nil {
		return workloadContract{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return workloadContract{}, errors.New("tracked workload contract must contain exactly one JSON value")
	}
	return contract, nil
}

func expectedWorkloadContract() workloadContract {
	return workloadContract{
		SchemaVersion: 1,
		Workload: contractWorkload{
			ID:           "cliproxy-m5",
			PhysicalHost: "m5",
			RunsOn:       "m5",
			Lifecycle:    "always_on",
			Runtime: contractRuntime{
				Kind:          "docker_compose",
				ComposeFile:   "docker-compose.yml",
				Service:       "cli-proxy-api",
				ContainerName: "cli-proxy-api",
				Image:         pinnedImage,
				RestartPolicy: "always",
			},
			ExpectedLANExposure: []contractExposure{
				{Protocol: "tcp", Port: 8317, Scope: "loopback", Purpose: "openai_compatible_api"},
				{Protocol: "tcp", Port: 8085, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 1455, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 54545, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 51121, Scope: "loopback", Purpose: "oauth_callback"},
				{Protocol: "tcp", Port: 11451, Scope: "loopback", Purpose: "oauth_callback"},
			},
			Health: contractHealth{
				Docker: contractDockerHealth{
					ContainerName:  "cli-proxy-api",
					ExpectedState:  "running",
					ExpectedHealth: "healthy",
				},
				HTTP: contractHTTPHealth{
					Scheme:         "http",
					Host:           "127.0.0.1",
					Port:           8317,
					Path:           "/healthz",
					ExpectedStatus: 200,
				},
			},
		},
	}
}

func exposureSet(exposures []contractExposure) (map[contractExposure]struct{}, error) {
	set := make(map[contractExposure]struct{}, len(exposures))
	for _, exposure := range exposures {
		if _, duplicate := set[exposure]; duplicate {
			return nil, errDuplicateExposureTuple
		}
		set[exposure] = struct{}{}
	}
	return set, nil
}

func validateWorkloadContract(contract workloadContract) error {
	expected := expectedWorkloadContract()
	contractExposures, err := exposureSet(contract.Workload.ExpectedLANExposure)
	if err != nil {
		return err
	}
	expectedExposures, err := exposureSet(expected.Workload.ExpectedLANExposure)
	if err != nil {
		return err
	}
	contract.Workload.ExpectedLANExposure = nil
	expected.Workload.ExpectedLANExposure = nil
	if !reflect.DeepEqual(contract, expected) || !reflect.DeepEqual(contractExposures, expectedExposures) {
		return errors.New("tracked workload contract does not match the secret-free CLIProxy owner contract")
	}
	return nil
}

func composePublishedPorts(composeText string) ([]string, error) {
	lines := strings.Split(composeText, "\n")
	portsLine := -1
	for index, line := range lines {
		trimmed := strings.TrimSpace(line)
		indent := len(line) - len(strings.TrimLeft(line, " "))
		if indent != 4 || !strings.HasPrefix(trimmed, "ports:") {
			continue
		}
		if portsLine != -1 || trimmed != "ports:" {
			return nil, errNonContractedPublishedPort
		}
		portsLine = index
	}
	if portsLine == -1 {
		return nil, errNonContractedPublishedPort
	}

	ports := make([]string, 0)
	for _, line := range lines[portsLine+1:] {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " "))
		if indent <= 4 {
			break
		}
		if indent != 6 || !strings.HasPrefix(trimmed, "- ") {
			return nil, errNonContractedPublishedPort
		}
		published := strings.TrimSpace(strings.TrimPrefix(trimmed, "- "))
		if len(published) < 2 || published[0] != '"' || published[len(published)-1] != '"' {
			return nil, errNonContractedPublishedPort
		}
		published, err := strconv.Unquote(published)
		if err != nil || published == "" {
			return nil, errNonContractedPublishedPort
		}
		ports = append(ports, published)
	}
	return ports, nil
}

func validateComposeContract(composeText string, contract workloadContract) error {
	requiredComposeFragments := []string{
		"  cli-proxy-api:\n",
		"    image: ${CLI_PROXY_IMAGE:-" + contract.Workload.Runtime.Image + "}\n",
		"    container_name: " + contract.Workload.Runtime.ContainerName + "\n",
		"      - \"127.0.0.1:8317:8317\"\n",
		"      - \"127.0.0.1:8085:8085\"\n",
		"      - \"127.0.0.1:1455:1455\"\n",
		"      - \"127.0.0.1:54545:54545\"\n",
		"      - \"127.0.0.1:51121:51121\"\n",
		"      - \"127.0.0.1:11451:11451\"\n",
		"        - CMD-SHELL\n",
		"/dev/tcp/127.0.0.1/8317",
		"GET /healthz HTTP/1.1",
		`[[ "$$status" == *" 200 "* ]]`,
		"      interval: 30s\n",
		"      timeout: 5s\n",
		"      retries: 3\n",
		"      start_period: 10s\n",
		"    restart: " + contract.Workload.Runtime.RestartPolicy + "\n",
	}
	for _, expected := range requiredComposeFragments {
		if !strings.Contains(composeText, expected) {
			return errors.New("tracked Compose file does not match the secret-free owner contract")
		}
	}
	publishedPorts, err := composePublishedPorts(composeText)
	if err != nil {
		return err
	}
	expectedPorts := make(map[string]struct{}, len(contract.Workload.ExpectedLANExposure))
	for _, exposure := range contract.Workload.ExpectedLANExposure {
		port := strconv.Itoa(exposure.Port)
		expectedPorts["127.0.0.1:"+port+":"+port] = struct{}{}
	}
	actualPorts := make(map[string]struct{}, len(publishedPorts))
	for _, published := range publishedPorts {
		if _, duplicate := actualPorts[published]; duplicate {
			return errNonContractedPublishedPort
		}
		if _, contracted := expectedPorts[published]; !contracted {
			return errNonContractedPublishedPort
		}
		actualPorts[published] = struct{}{}
	}
	if len(actualPorts) != len(expectedPorts) {
		return errNonContractedPublishedPort
	}
	return nil
}

func trackedContractJSON(t *testing.T) string {
	t.Helper()
	contractData, err := os.ReadFile("workload-contract.json")
	if err != nil {
		t.Fatalf("read tracked workload contract: %v", err)
	}
	return string(contractData)
}

func replaceContractJSON(t *testing.T, raw, old, replacement string) string {
	t.Helper()
	if !strings.Contains(raw, old) {
		t.Fatal("contract test fixture replacement target is missing")
	}
	return strings.Replace(raw, old, replacement, 1)
}

func requireContractDecodeError(t *testing.T, raw, expected string, forbidden ...string) {
	t.Helper()
	_, err := decodeWorkloadContract([]byte(raw))
	if err == nil {
		t.Fatal("invalid tracked workload contract was accepted")
	}
	if err.Error() != expected {
		t.Fatal("contract diagnostic is not deterministic and secret-safe")
	}
	for _, value := range forbidden {
		if strings.Contains(err.Error(), value) {
			t.Fatal("contract diagnostic exposed an input key or value")
		}
	}
}

func TestDecodeWorkloadContractRejectsDuplicateKeys(t *testing.T) {
	const secretLikeCanary = "sk-live-token-canary-not-a-real-secret"
	tests := []struct {
		name string
		raw  string
	}{
		{
			name: "top-level key",
			raw:  `{"schema_version":1,"schema_version":1,"workload":{}}`,
		},
		{
			name: "nested allowed key with secret-like earlier value",
			raw: `{"schema_version":1,"workload":{"runtime":{` +
				`"image":"` + secretLikeCanary + `","image":"safe"}}}`,
		},
		{
			name: "key inside array object",
			raw: `{"schema_version":1,"workload":{"expected_lan_exposure":[` +
				`{"port":8317,"port":8317}]}}`,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := decodeWorkloadContract([]byte(test.raw))
			if err == nil {
				t.Fatal("duplicate JSON object member was accepted")
			}
			if strings.Contains(err.Error(), secretLikeCanary) {
				t.Fatal("duplicate-key diagnostic exposed a JSON value")
			}
			if err.Error() != "duplicate JSON object member" {
				t.Fatal("duplicate-key diagnostic is not deterministic and secret-safe")
			}
		})
	}
}

func TestDecodeWorkloadContractRejectsNonIntegerSchemaVersion(t *testing.T) {
	for _, test := range []struct {
		name          string
		schemaVersion string
	}{
		{name: "boolean", schemaVersion: "true"},
		{name: "float", schemaVersion: "1.0"},
	} {
		t.Run(test.name, func(t *testing.T) {
			raw := `{"schema_version":` + test.schemaVersion + `,"workload":{}}`
			if _, err := decodeWorkloadContract([]byte(raw)); err == nil {
				t.Fatal("non-integer schema_version was accepted")
			}
		})
	}
}

func TestDecodeWorkloadContractRejectsCaseAliasesAndUnknownFields(t *testing.T) {
	const secretLikeCanary = "sk-live-field-canary-not-a-real-secret"
	valid := trackedContractJSON(t)
	tests := []struct {
		name        string
		old         string
		replacement string
		forbidden   string
	}{
		{
			name:        "canonical null plus uppercase schema alias",
			old:         `"schema_version": 1`,
			replacement: `"schema_version": null, "SCHEMA_VERSION": 1`,
			forbidden:   "SCHEMA_VERSION",
		},
		{
			name:        "workload alias",
			old:         `"physical_host": "m5"`,
			replacement: `"PHYSICAL_HOST": "m5"`,
			forbidden:   "PHYSICAL_HOST",
		},
		{
			name:        "runtime alias",
			old:         `"compose_file": "docker-compose.yml"`,
			replacement: `"COMPOSE_FILE": "docker-compose.yml"`,
			forbidden:   "COMPOSE_FILE",
		},
		{
			name:        "exposure alias",
			old:         `"protocol": "tcp"`,
			replacement: `"PROTOCOL": "tcp"`,
			forbidden:   "PROTOCOL",
		},
		{
			name:        "health alias",
			old:         `"docker": {`,
			replacement: `"DOCKER": {`,
			forbidden:   "DOCKER",
		},
		{
			name:        "docker health alias",
			old:         `"expected_state": "running"`,
			replacement: `"EXPECTED_STATE": "running"`,
			forbidden:   "EXPECTED_STATE",
		},
		{
			name:        "http health alias",
			old:         `"expected_status": 200`,
			replacement: `"EXPECTED_STATUS": 200`,
			forbidden:   "EXPECTED_STATUS",
		},
		{
			name:        "unknown workload field",
			old:         `"id": "cliproxy-m5",`,
			replacement: `"id": "cliproxy-m5", "unexpected": "` + secretLikeCanary + `",`,
			forbidden:   secretLikeCanary,
		},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			raw := replaceContractJSON(t, valid, test.old, test.replacement)
			requireContractDecodeError(
				t,
				raw,
				"contract contains an invalid JSON field name",
				test.forbidden,
				secretLikeCanary,
			)
		})
	}
}

func TestDecodeWorkloadContractRejectsMissingRequiredFields(t *testing.T) {
	valid := trackedContractJSON(t)
	for _, test := range []struct {
		name    string
		missing string
	}{
		{name: "root field", missing: "  \"schema_version\": 1,\n"},
		{name: "nested field", missing: "      \"compose_file\": \"docker-compose.yml\",\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			raw := replaceContractJSON(t, valid, test.missing, "")
			requireContractDecodeError(
				t,
				raw,
				"contract is missing a required JSON field",
			)
		})
	}
}

func TestDecodeWorkloadContractRejectsCanonicalNull(t *testing.T) {
	valid := trackedContractJSON(t)
	raw := replaceContractJSON(t, valid, `"schema_version": 1`, `"schema_version": null`)
	requireContractDecodeError(t, raw, "contract contains a null JSON value")
}

func TestValidateWorkloadContractTreatsExposuresAsUnorderedSet(t *testing.T) {
	contract := expectedWorkloadContract()
	exposures := contract.Workload.ExpectedLANExposure
	contract.Workload.ExpectedLANExposure = append(
		append([]contractExposure{}, exposures[3:]...),
		exposures[:3]...,
	)

	if err := validateWorkloadContract(contract); err != nil {
		t.Fatal("reordered expected_lan_exposure was rejected")
	}
}

func TestValidateWorkloadContractRejectsDuplicateExposureTuple(t *testing.T) {
	contract := expectedWorkloadContract()
	contract.Workload.ExpectedLANExposure = append(
		contract.Workload.ExpectedLANExposure,
		contract.Workload.ExpectedLANExposure[0],
	)

	err := validateWorkloadContract(contract)
	if err == nil {
		t.Fatal("duplicate expected_lan_exposure tuple was accepted")
	}
	if err.Error() != "duplicate expected_lan_exposure tuple" {
		t.Fatal("duplicate-exposure diagnostic is not deterministic and secret-safe")
	}
}

func TestValidateComposeContractRejectsAdditionalPublishedPort(t *testing.T) {
	composeData, err := os.ReadFile("../docker-compose.yml")
	if err != nil {
		t.Fatalf("read tracked Compose file: %v", err)
	}
	const contractedPort = `      - "127.0.0.1:11451:11451"`
	const extraPort = `      - "0.0.0.0:9999:9999"`
	adversarial := strings.Replace(
		string(composeData),
		contractedPort,
		contractedPort+"\n"+extraPort,
		1,
	)
	if adversarial == string(composeData) {
		t.Fatal("Compose test fixture replacement target is missing")
	}

	err = validateComposeContract(adversarial, expectedWorkloadContract())
	if err == nil {
		t.Fatal("additional published Compose port was accepted")
	}
	if strings.Contains(err.Error(), "9999") || strings.Contains(err.Error(), "0.0.0.0") {
		t.Fatal("Compose diagnostic exposed an untrusted port value")
	}
	if err.Error() != "tracked Compose file contains a non-contracted published port" {
		t.Fatal("additional-port diagnostic is not deterministic and secret-safe")
	}
}

func TestWorkloadContractMatchesCompose(t *testing.T) {
	contractData, err := os.ReadFile("workload-contract.json")
	if err != nil {
		t.Fatalf("read tracked workload contract: %v", err)
	}
	contract, err := decodeWorkloadContract(contractData)
	if err != nil {
		t.Fatalf("decode tracked workload contract: %v", err)
	}
	if err := validateWorkloadContract(contract); err != nil {
		t.Fatal(err)
	}

	composeData, err := os.ReadFile("../docker-compose.yml")
	if err != nil {
		t.Fatalf("read tracked Compose file: %v", err)
	}
	if err := validateComposeContract(string(composeData), contract); err != nil {
		t.Fatal(err)
	}
}
