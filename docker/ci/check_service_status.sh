# A method to check that runit services are running inside the container
function check_service_status()
{
  # The container to run the command in
  container="$1"

  # The location of the runit service should be passed.
  service="$2"

  # Check the status of the service.
  service_status=$(docker exec "$container" /bin/bash -c "sv check $service")

  # Get the exit code for the sv call.
  sv_status=$?

  if [[ "$sv_status" != 0 || "$service_status" =~ down ]]; then
    echo "  FAIL: $service is not running"
    exit 1
  else
    echo "  OK"
  fi
}
