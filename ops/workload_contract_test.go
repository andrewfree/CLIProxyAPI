package ops

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"strings"
	"testing"
)

const pinnedImage = "eceasy/cli-proxy-api@sha256:e75d910b1fa7ef7e05cf3d5b06c3fbaa467eaf70cea0232cb37e4ede19e978ed"

var (
	errDuplicateJSONObjectMember = errors.New("duplicate JSON object member")
	errDuplicateExposureTuple    = errors.New("duplicate expected_lan_exposure tuple")
	errInvalidJSONStructure      = errors.New("invalid JSON structure")
)

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

func decodeWorkloadContract(contractData []byte) (workloadContract, error) {
	if err := rejectDuplicateJSONKeys(contractData); err != nil {
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
	composeText := string(composeData)
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
			t.Fatal("tracked Compose file does not match the secret-free owner contract")
		}
	}
	if strings.Count(composeText, `      - "127.0.0.1:`) != len(contract.Workload.ExpectedLANExposure) {
		t.Fatal("tracked Compose file must contain exactly six contracted loopback bindings")
	}
	for _, forbidden := range []string{
		`      - "8317:8317"`,
		`      - "8085:8085"`,
		`      - "1455:1455"`,
		`      - "54545:54545"`,
		`      - "51121:51121"`,
		`      - "11451:11451"`,
	} {
		if strings.Contains(composeText, forbidden) {
			t.Fatal("tracked Compose file contains a non-loopback form of a contracted port")
		}
	}
}
