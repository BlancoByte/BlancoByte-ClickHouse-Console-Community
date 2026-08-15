import click
from clickhouse_driver import Client
import os
import getpass

# For text color
class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

@click.command()
@click.option('--database', prompt='Enter the database name', help='Database name')
@click.option('--table', prompt='Enter the table name', help='Table name')
@click.option('--branch-name', prompt='Enter the branch name', help='Branch name')
@click.option('--username', prompt='Enter the ClickHouse username', help='ClickHouse username')
@click.option('--password', prompt='Enter the ClickHouse password', hide_input=True, help='ClickHouse password')
@click.option('--hostname', prompt='Enter the ClickHouse hostname', help='ClickHouse hostname')
@click.option('--port', prompt='Enter the ClickHouse port', help='ClickHouse port')
def main(database, table, branch_name, username, password, hostname, port):
    # Create a ClickHouse client
    client = Client(host=hostname, port=port, user=username, password=password)

    try:
        # Freeze the table
        freeze_table(client, database, table, branch_name)

        # Get table information
        get_table_information(client, database, table)

        # Edit file
        edit_file(database, table, branch_name)

        # Execute the SQL script to create the table
        execute_sql_script(client, database, table, branch_name)

        # Attach frozen parts
        attach_parts(client, database, table, branch_name)

    except Exception as e:
        print(bcolors.FAIL + f"Error: {str(e)}" + bcolors.ENDC)

    finally:
        # Close the ClickHouse connection
        client.disconnect()

def freeze_table(client, database, table, branch_name):
    freeze_name = f"{database}.{table}_{branch_name}"
    query = f"ALTER TABLE {database}.{table} FREEZE WITH NAME '{freeze_name}'"
    try:
        client.execute(query)
        print(bcolors.OKGREEN + f"The table {database}.{table} has been frozen with name '{freeze_name}'" + bcolors.ENDC)
    except Exception as e:
        print(bcolors.FAIL + f"Error occurred while freezing the table: {e}" + bcolors.ENDC)

def get_table_information(client, database, table):
    try:
        # Execute the query and fetch results
        query = f"SHOW CREATE TABLE {database}.{table}"
        result = client.execute(query)

        output_file = f"/tmp/{database}.{table}.sql"
        # Save the result to a text file
        with open(output_file, "w") as file:
            for row in result:
                file.write(",".join(str(value) for value in row) + "\n")

        print(bcolors.OKGREEN + f"Table information is saved into {output_file}" + bcolors.ENDC)
    except Exception as e:
        print(bcolors.FAIL + f"Error occurred while getting table information: {e}" + bcolors.ENDC)

def edit_file(database, table, branch_name):
    file_path = f"/tmp/{database}.{table}.sql"
    new_file_path = f"/tmp/{database}.{table}_{branch_name}.sql"

    try:
        with open(file_path, "r") as file:
            lines = file.readlines()

        if len(lines) > 0:
            lines[0] = f"CREATE TABLE {database}.{table}_{branch_name}\n"

        with open(new_file_path, "w") as file:
            file.writelines(lines)

        print(bcolors.OKGREEN + f"New Table information is saved into {new_file_path}" + bcolors.ENDC)

    except FileNotFoundError:
        print(bcolors.FAIL + f"Error: The file {file_path} does not exist." + bcolors.ENDC)

    except IOError:
        print(bcolors.FAIL + f"Error: An error occurred while reading or writing the file." + bcolors.ENDC)

    except Exception as e:
        print(bcolors.FAIL + f"Error: {str(e)}" + bcolors.ENDC)

def execute_sql_script(client, database, table, branch_name):
    sql_file = f"/tmp/{database}.{table}_{branch_name}.sql"
    try:
        with open(sql_file, "r") as file:
            sql_script = file.read()

        # Execute the SQL script to create the table
        client.execute(sql_script)

        print(bcolors.OKGREEN + f"Table {database}.{table}_{branch_name} created successfully." + bcolors.ENDC)

    except FileNotFoundError:
        print(bcolors.FAIL + f"Error: The file {sql_file} does not exist." + bcolors.ENDC)

    except IOError:
        print(bcolors.FAIL + f"Error: An error occurred while reading the file {sql_file}." + bcolors.ENDC)

    except Exception as e:
        print(bcolors.FAIL + f"Error: {str(e)}" + bcolors.ENDC)

def attach_parts(client, database, table, branch_name):
    full_path = "/var/lib/clickhouse/shadow/" + database + "%2E" + table + "_" + branch_name + "/store"

    try:
        pathRoot = full_path
        pathFirst = os.path.join(pathRoot, os.listdir(pathRoot)[0])
        pathSecond = os.path.join(pathFirst, os.listdir(pathFirst)[0])
        print(pathSecond)

        # Set the destination path
        destination = "/var/lib/clickhouse/data/" + database + "/" + table + "_" + branch_name + "/detached/"
        full_destination = destination
        print(full_destination)

        # Build the rsync command
        command = f"rsync -av --chmod=ugo=rwX {pathSecond}/ {full_destination}"

        # Execute the rsync command
        return_code = os.system(command)

        if return_code == 0:
            print(bcolors.OKGREEN + f"Attach copy successfully done." + bcolors.ENDC)
        else:
            print(bcolors.FAIL + f"Error: An error occurred while executing the rsync command." + bcolors.ENDC)

    except FileNotFoundError:
        print(bcolors.FAIL + f"Error: The directory {full_path} does not exist." + bcolors.ENDC)

    except IndexError:
        print(bcolors.FAIL + f"Error: The directory {full_path} is empty or does not contain the expected subdirectories." + bcolors.ENDC)

    except Exception as e:
        print(bcolors.FAIL + f"Error: {str(e)}" + bcolors.ENDC)

if __name__ == '__main__':
    main()
